from collections import Counter
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)

from app.models import PermissionType, RequestStatus, UserRole
from app.models.classes import ClassModel, MemberModel
from app.models.permission import PermissionModel
from app.models.user import UserModel
from app.permissions.service import categorize_reason
from app.utils.security import current_user, roles_required

hod_bp = Blueprint('hod', __name__, url_prefix='/hod')


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
    from app.models.firestore import store

    user = current_user()
    search = (request.args.get('q') or '').strip()

    rows = store.documents('users', role=UserRole.STUDENT.value, is_active=True)

    if search:
        needle = search.upper()
        rows = [row for row in rows
                if needle in (row.get('roll_number') or '').upper()
                or needle in (row.get('name') or '').upper()]

    # The counts were five correlated subqueries per student. They are counted
    # over two reads of the collections instead: one pass each, then a lookup.
    # Firestore has no subquery, and one read per student would be worse.
    per_student = Counter()
    class_counts = Counter()
    for row in store.documents('permission_requests'):
        per_student[row.get('student_id')] += 1
    for row in store.documents('permission_requests',
                               status=RequestStatus.APPROVED.value):
        per_student[(row.get('student_id'), 'approved')] += 1
    for row in store.documents('permission_requests',
                               status=RequestStatus.PENDING.value):
        per_student[(row.get('student_id'), 'pending')] += 1
    for row in store.documents('permission_requests',
                               status=RequestStatus.REJECTED.value):
        per_student[(row.get('student_id'), 'rejected')] += 1
    for row in store.documents('class_members'):
        class_counts[row.get('student_id')] += 1

    students = []
    for row in rows:
        student_id = row.get('id')
        students.append({
            'id': student_id,
            'name': row.get('name'),
            'roll_number': row.get('roll_number'),
            'email': row.get('email'),
            'phone': row.get('phone'),
            'total': per_student[student_id],
            'approved': per_student[(student_id, 'approved')],
            'pending': per_student[(student_id, 'pending')],
            'rejected': per_student[(student_id, 'rejected')],
            'classes': class_counts[student_id],
        })
    students.sort(key=lambda s: s['roll_number'] or '')
    students = students[:200]

    return render_template(
        'hod/students.html', user=user, students=students, search=search
    )


@hod_bp.route('/faculty')
@roles_required(UserRole.HOD)
def faculty_workload():
    """Open queue and decision counts per lecturer."""
    from app.models.firestore import store

    user = current_user()

    # Counted per lecturer in Python. The old query ran a subquery per lecturer
    # for each of five counts; Firestore cannot do that at all, and one pass over
    # each collection answers every column.
    assigned = Counter()
    for status in (RequestStatus.PENDING, RequestStatus.APPROVED,
                   RequestStatus.REJECTED):
        for row in store.documents('permission_requests',
                                   status=status.value):
            faculty_id = row.get('assigned_faculty_id')
            if faculty_id:
                assigned[(faculty_id, status.value)] += 1
    decisions = Counter()
    for row in store.documents('approval_history'):
        decisions[row.get('faculty_id')] += 1
    class_counts = Counter()
    for row in store.documents('class_groups'):
        class_counts[row.get('faculty_id')] += 1

    lecturers = [
        {
            'id': user_row['id'],
            'name': user_row.get('name'),
            'email': user_row.get('email'),
            'pending': assigned[(user_row['id'], RequestStatus.PENDING.value)],
            'approved': assigned[(user_row['id'], RequestStatus.APPROVED.value)],
            'rejected': assigned[(user_row['id'], RequestStatus.REJECTED.value)],
            'decisions': decisions[user_row['id']],
            'classes': class_counts[user_row['id']],
        }
        for user_row in store.documents('users',
                                        role=UserRole.LECTURER.value,
                                        is_active=True)
    ]
    lecturers.sort(key=lambda l: (-l['pending'], l['name'] or ''))

    return render_template(
        'hod/faculty.html', user=user, lecturers=lecturers
    )


@hod_bp.route('/classes')
@roles_required(UserRole.HOD)
def classes():
    """Every class across all lecturers."""
    from app.models.firestore import store

    user = current_user()
    MemberModel.relink_unresolved()

    faculty_names = {
        row['id']: row.get('name')
        for row in store.documents('users')
    }
    member_counts = Counter()
    linked_counts = Counter()
    for row in store.documents('class_members'):
        member_counts[row.get('class_id')] += 1
        if row.get('student_id'):
            linked_counts[row.get('class_id')] += 1

    groups = [
        {
            'id': row['id'],
            'name': row.get('name'),
            'section_code': row.get('section_code'),
            'academic_year': row.get('academic_year'),
            'faculty_id': row.get('faculty_id'),
            'created_at': row.get('created_at'),
            'faculty_name': faculty_names.get(row.get('faculty_id')),
            'member_count': member_counts[row['id']],
            'linked_count': linked_counts[row['id']],
        }
        for row in store.documents('class_groups')
    ]
    # Lecturer name ascending, then newest class first -- the order the query
    # used to state. Sorted twice on purpose: Python's sort is stable, so the
    # first pass fixes the recency and the second keeps it inside each lecturer.
    groups.sort(key=lambda g: _sort_key(g['created_at']), reverse=True)
    groups.sort(key=lambda g: g['faculty_name'] or '')

    return render_template('hod/classes.html', user=user, classes=groups)


def _sort_key(value) -> str:
    """A timestamp or date as text, so two orderings of different types compare."""
    return value.isoformat() if hasattr(value, 'isoformat') else str(value or '')


@hod_bp.route('/reports')
@roles_required(UserRole.HOD)
def reports():
    """Department analytics: trends, reasons, types and per-faculty volume."""
    from app.models.firestore import store

    user = current_user()
    stats = PermissionModel.get_stats_for_hod()

    requests = store.documents('permission_requests')

    # The trend is bucketed in Python. Timestamps come back from the store already
    # converted to REPORT_TIMEZONE, so the calendar day here is the same day the
    # AT TIME ZONE cast used to produce -- and the 23:30 IST submission still
    # charts under the day it was filed.
    by_day = Counter()
    for row in requests:
        created = row.get('created_at')
        if isinstance(created, datetime):
            by_day[created.date()] += 1

    by_faculty = []
    for row in store.documents('users', role=UserRole.LECTURER.value,
                               is_active=True):
        by_faculty.append({
            'faculty_name': row.get('name'),
            'assigned': sum(1 for r in requests
                            if r.get('assigned_faculty_id') == row['id']),
        })
    by_faculty.sort(key=lambda f: (-f['assigned'], f['faculty_name'] or ''))

    totals = Counter()
    approved = Counter()
    students = {}
    for row in requests:
        student_id = row.get('student_id')
        totals[student_id] += 1
        if row.get('status') == RequestStatus.APPROVED.value:
            approved[student_id] += 1
    for student_id in {r.get('student_id') for r in requests if r.get('student_id')}:
        row = store.get('users', student_id)
        if row:
            students[student_id] = row

    top_students = [
        {
            'roll_number': (students[student_id] or {}).get('roll_number'),
            'name': (students[student_id] or {}).get('name'),
            'total': count,
            'approved': approved[student_id],
        }
        for student_id, count in totals.most_common(10)
        if student_id in students
    ]

    # Build a continuous 30-day series so the chart has no gaps.
    start = date.today() - timedelta(days=29)
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