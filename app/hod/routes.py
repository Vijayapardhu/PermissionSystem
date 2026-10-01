from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, current_app, flash, redirect, render_template, request, url_for,
)

from app.models import PermissionType, RequestStatus, UserRole
from app.models.classes import ClassModel, MemberModel
from app.models.database import db
from app.models.permission import PermissionModel
from app.models.user import UserModel
from app.permissions.service import categorize_reason
from app.utils.security import current_user, roles_required

hod_bp = Blueprint('hod', __name__, url_prefix='/hod')


@contextmanager
def db_cursor(dictionary=True):
    with db.get_cursor(dictionary=dictionary) as cursor:
        yield cursor


def _parse(value, default=None):
    """Backwards-compatible alias for _parse_date."""
    return _parse_date(value, default)


@hod_bp.route('/dashboard')
@roles_required(UserRole.HOD)
def dashboard():
    user = current_user()

    status_filter = None
    if request.args.get('status'):
        try:
            status_filter = RequestStatus(request.args['status'].upper())
        except ValueError:
            status_filter = None

    type_filter = None
    if request.args.get('type'):
        try:
            type_filter = PermissionType(request.args['type'].upper())
        except ValueError:
            type_filter = None

    records = PermissionModel.find_all_for_hod(
        status=status_filter, permission_type=type_filter, limit=500
    )

    stats = PermissionModel.get_stats_for_hod()
    approved_today = PermissionModel.get_today_approved()

    total = stats['total'] or 1
    # Note: the payload key is "series", not "values" -- a dict attribute named
    # "values" would shadow the key in Jinja and serialise the bound method.
    status_distribution = {
        'labels': ['Approved', 'Rejected', 'Pending'],
        'series': [
            round((stats['approved'] / total) * 100, 1),
            round((stats['rejected'] / total) * 100, 1),
            round((stats['pending'] / total) * 100, 1),
        ],
        'counts': [stats['approved'], stats['rejected'], stats['pending']],
    }

    type_distribution = {
        'labels': ['Leave Permission', 'Classroom Permission'],
        'series': [stats['leave_count'], stats['classroom_count']],
    }

    reason_counts = Counter(categorize_reason(record.reason) for record in records)
    reason_chart = {
        'labels': list(reason_counts.keys()) or ['No data'],
        'series': list(reason_counts.values()) or [0],
    }

    trend = _weekly_trend()

    return render_template(
        'hod/dashboard.html',
        user=user,
        stats=stats,
        records=records,
        approved_today=approved_today,
        status_chart=status_distribution,
        type_chart=type_distribution,
        reason_chart=reason_chart,
        trend_chart=trend,
        status_filter=status_filter,
        type_filter=type_filter,
    )


def _weekly_trend():
    """Requests created per day over the last seven days."""
    start = date.today() - timedelta(days=6)
    labels, values = [], []
    for offset in range(7):
        day = start + timedelta(days=offset)
        day_requests = PermissionModel.find_all_for_hod(
            start_date=day, end_date=day, limit=500
        )
        labels.append(day.strftime('%d %b'))
        values.append(len(day_requests))
    return {'labels': labels, 'series': values}


@hod_bp.route('/requests')
@roles_required(UserRole.HOD)
def requests():
    """Every request in the department, with filters."""
    user = current_user()

    status_filter = _status_filter(request.args.get('status'))
    type_filter = _type_filter(request.args.get('type'))
    date_from = _parse_date(request.args.get('from'), None)
    date_to = _parse_date(request.args.get('to'), None)
    search = (request.args.get('q') or '').strip()

    records = _filtered_requests(status_filter, type_filter,
                                 date_from, date_to, search, limit=400)

    return render_template(
        'hod/requests.html',
        user=user,
        requests=records,
        status_filter=status_filter,
        type_filter=type_filter,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )


@hod_bp.route('/students')
@roles_required(UserRole.HOD)
def students():
    """Student directory with permission counts."""
    user = current_user()
    search = (request.args.get('q') or '').strip()

    with db_cursor() as cursor:
        if search:
            like = f'%{search}%'
            cursor.execute(
                """SELECT u.*,
                          (SELECT COUNT(*) FROM permission_requests pr WHERE pr.student_id = u.id) AS total,
                          (SELECT COUNT(*) FROM permission_requests pr WHERE pr.student_id = u.id AND pr.status='APPROVED') AS approved,
                          (SELECT COUNT(*) FROM permission_requests pr WHERE pr.student_id = u.id AND pr.status='PENDING') AS pending,
                          (SELECT COUNT(*) FROM permission_requests pr WHERE pr.student_id = u.id AND pr.status='REJECTED') AS rejected,
                          (SELECT COUNT(*) FROM class_members m WHERE m.student_id = u.id) AS classes
                   FROM users u
                   WHERE u.role = 'STUDENT' AND u.is_active = TRUE
                     AND (u.roll_number ILIKE %s OR u.name ILIKE %s)
                   ORDER BY u.roll_number LIMIT 200""",
                (like, like),
            )
        else:
            cursor.execute(
                """SELECT u.*,
                          (SELECT COUNT(*) FROM permission_requests pr WHERE pr.student_id = u.id) AS total,
                          (SELECT COUNT(*) FROM permission_requests pr WHERE pr.student_id = u.id AND pr.status='APPROVED') AS approved,
                          (SELECT COUNT(*) FROM permission_requests pr WHERE pr.student_id = u.id AND pr.status='PENDING') AS pending,
                          (SELECT COUNT(*) FROM permission_requests pr WHERE pr.student_id = u.id AND pr.status='REJECTED') AS rejected,
                          (SELECT COUNT(*) FROM class_members m WHERE m.student_id = u.id) AS classes
                   FROM users u
                   WHERE u.role = 'STUDENT' AND u.is_active = TRUE
                   ORDER BY u.roll_number LIMIT 200"""
            )
        students = cursor.fetchall()

    return render_template(
        'hod/students.html', user=user, students=students, search=search
    )


@hod_bp.route('/faculty')
@roles_required(UserRole.HOD)
def faculty_workload():
    """Open queue and decision counts per lecturer."""
    user = current_user()

    with db_cursor() as cursor:
        cursor.execute(
            """SELECT u.id, u.name, u.email,
                      (SELECT COUNT(*) FROM permission_requests pr
                        WHERE pr.assigned_faculty_id = u.id AND pr.status = 'PENDING') AS pending,
                      (SELECT COUNT(*) FROM permission_requests pr
                        WHERE pr.assigned_faculty_id = u.id AND pr.status = 'APPROVED') AS approved,
                      (SELECT COUNT(*) FROM permission_requests pr
                        WHERE pr.assigned_faculty_id = u.id AND pr.status = 'REJECTED') AS rejected,
                      (SELECT COUNT(*) FROM approval_history ah
                        WHERE ah.faculty_id = u.id) AS decisions,
                      (SELECT COUNT(*) FROM class_groups c
                        WHERE c.faculty_id = u.id) AS classes
               FROM users u
               WHERE u.role = 'LECTURER' AND u.is_active = TRUE
               ORDER BY pending DESC, u.name"""
        )
        lecturers = cursor.fetchall()

    return render_template(
        'hod/faculty.html', user=user, lecturers=lecturers
    )


@hod_bp.route('/classes')
@roles_required(UserRole.HOD)
def classes():
    """Every class across all lecturers."""
    user = current_user()
    MemberModel.relink_unresolved()

    with db_cursor() as cursor:
        cursor.execute(
            """SELECT c.*, u.name AS faculty_name,
                      (SELECT COUNT(*) FROM class_members m WHERE m.class_id = c.id) AS member_count,
                      (SELECT COUNT(*) FROM class_members m
                        WHERE m.class_id = c.id AND m.student_id IS NOT NULL) AS linked_count
               FROM class_groups c
               JOIN users u ON u.id = c.faculty_id
               ORDER BY u.name, c.created_at DESC"""
        )
        groups = cursor.fetchall()

    return render_template('hod/classes.html', user=user, classes=groups)


@hod_bp.route('/reports')
@roles_required(UserRole.HOD)
def reports():
    """Department analytics: trends, reasons, types and per-faculty volume."""
    user = current_user()
    stats = PermissionModel.get_stats_for_hod()

    tz = current_app.config['REPORT_TIMEZONE']
    with db_cursor() as cursor:
        # Cast through REPORT_TIMEZONE, not the session zone. The session runs in
        # UTC, so without this a 23:30 IST submission would chart under the
        # following day.
        cursor.execute(
            f"""SELECT (created_at AT TIME ZONE '{tz}')::date AS day, COUNT(*) AS n
               FROM permission_requests
               WHERE (created_at AT TIME ZONE '{tz}')::date
                     >= (now() AT TIME ZONE '{tz}')::date - INTERVAL '29 days'
               GROUP BY (created_at AT TIME ZONE '{tz}')::date ORDER BY day"""
        )
        rows = cursor.fetchall()

        cursor.execute(
            """SELECT f.name AS faculty_name,
                      (SELECT COUNT(*) FROM permission_requests pr
                        WHERE pr.assigned_faculty_id = f.id) AS assigned
               FROM users f WHERE f.role = 'LECTURER' AND f.is_active = TRUE
               ORDER BY assigned DESC"""
        )
        by_faculty = cursor.fetchall()

        cursor.execute(
            """SELECT u.roll_number, u.name,
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE pr.status = 'APPROVED') AS approved
               FROM permission_requests pr JOIN users u ON u.id = pr.student_id
               GROUP BY u.id, u.roll_number, u.name
               ORDER BY total DESC LIMIT 10"""
        )
        top_students = cursor.fetchall()

    # Build a continuous 30-day series so the chart has no gaps.
    start = date.today() - timedelta(days=29)
    by_day = {r['day']: r['n'] for r in rows}
    labels, series = [], []
    for offset in range(30):
        day = start + timedelta(days=offset)
        labels.append(day.strftime('%d %b'))
        series.append(by_day.get(day, 0))

    return render_template(
        'hod/reports.html',
        user=user,
        stats=stats,
        trend={'labels': labels, 'series': series},
        by_faculty=by_faculty,
        top_students=top_students,
    )


# ---------- helpers ----------

def _parse_date(value, default=None):
    """Parse a YYYY-MM-DD query parameter, falling back to a default."""
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return default


def _status_filter(value):
    if not value:
        return None
    try:
        return RequestStatus(value.upper())
    except ValueError:
        return None


def _type_filter(value):
    if not value:
        return None
    try:
        return PermissionType(value.upper())
    except ValueError:
        return None


def _filtered_requests(status_filter, type_filter, date_from, date_to,
                      search, limit=300):
    from app.faculty.routes import _filtered_requests as shared
    return shared(status_filter, type_filter, date_from, date_to, search, limit)


@hod_bp.route('/report/print')
@roles_required(UserRole.HOD)
def print_report():
    user = current_user()

    report_date = _parse_date(request.args.get('date'), date.today())
    status_filter = None
    if request.args.get('status'):
        try:
            status_filter = RequestStatus(request.args['status'].upper())
        except ValueError:
            status_filter = None

    records = PermissionModel.find_all_for_hod(status=status_filter, limit=1000)
    on_day = [
        record for record in records
        if record.start_date and record.start_date <= report_date
        and record.end_date and record.end_date >= report_date
    ]

    stats = PermissionModel.get_stats_for_hod()

    return render_template(
        'hod/print_report.html',
        user=user,
        records=on_day,
        report_date=report_date,
        stats=stats,
        status_filter=status_filter,
    )