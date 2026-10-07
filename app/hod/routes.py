from collections import Counter
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)

from app.models import (
    ApprovalAction, PermissionType, RequestStatus, UserRole,
)
from app.models.classes import ClassModel, MemberModel
from app.models.permission import (
    ApprovalModel, PermissionModel, ProofModel, attach_members,
)
from app.models.settings import SettingsModel
from app.models.user import UserModel
from app.permissions.pages import permission_required
from app.permissions.service import (
    ValidationError, categorize_reason, hod_act_on_request,
)
from app.utils.roster import RosterError, parse_faculty_roster
from app.utils.security import current_user

hod_bp = Blueprint('hod', __name__, url_prefix='/hod')


def _parse(value, default=None):
    """Backwards-compatible alias for _parse_date."""
    return _parse_date(value, default)


def _approval_action(raw: str) -> ApprovalAction:
    """The posted decision, or a 400 rather than a silent default.

    Defaulting an unrecognised value to APPROVED would let a mistyped or
    tampered button grant a permission, which is the one outcome this whole stage
    exists to prevent.
    """
    try:
        return ApprovalAction((raw or '').strip().upper())
    except ValueError:
        abort(400, description='Choose whether to approve or reject.')


@hod_bp.route('/dashboard')
@permission_required('hod.dashboard')
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

    # One type is issued now, so a type split is a single bar. The shape is kept
    # because the chart reads `labels` and `series` and the dashboard template
    # renders it without knowing what is in it.
    type_distribution = {
        'labels': ['Activity Permission'],
        'series': [stats['classroom_count']],
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
@permission_required('hod.requests')
def requests():
    """Every request in the department, with filters.

    The HOD's working register: the queue awaiting their decision leads with
    one-click actions, and the filters (status, reviewer, dates, search) stay
    visible while scrolling so a long register stays usable.
    """
    user = current_user()

    status_filter = _status_filter(request.args.get('status'))
    type_filter = _type_filter(request.args.get('type'))
    date_from = _parse_date(request.args.get('from'), None)
    date_to = _parse_date(request.args.get('to'), None)
    search = (request.args.get('q') or '').strip()
    reviewer_id = request.args.get('reviewer', type=int)

    records = _filtered_requests(status_filter, type_filter,
                                 date_from, date_to, search, limit=400,
                                 faculty_id=reviewer_id)

    reviewers = [
        {'id': lecturer.id, 'name': lecturer.name}
        for lecturer in UserModel.get_lecturers()
    ]
    counts = {
        'awaiting': sum(1 for r in records
                        if r.status == RequestStatus.AWAITING_HOD),
        'pending': sum(1 for r in records
                       if r.status == RequestStatus.PENDING),
        'approved': sum(1 for r in records
                        if r.status == RequestStatus.APPROVED),
        'rejected': sum(1 for r in records
                        if r.status == RequestStatus.REJECTED),
    }

    return render_template(
        'hod/requests.html',
        user=user,
        requests=records,
        status_filter=status_filter,
        type_filter=type_filter,
        date_from=date_from,
        date_to=date_to,
        search=search,
        reviewers=reviewers,
        reviewer_id=reviewer_id,
        counts=counts,
    )


@hod_bp.route('/requests/<int:request_id>', methods=['GET'])
@permission_required('hod.request_detail')
def request_detail(request_id: int):
    """One request in full, with every student it covers.

    The HOD approves here rather than from the register row, because dropping a
    student off a group needs the whole membership on screen to be a decision
    rather than a guess.
    """
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        abort(404)
    attach_members([record])
    from app.faculty.routes import annotate_routes as _annotate
    _annotate([record])
    if record.assigned_faculty_id:
        reviewer = UserModel.find_by_id(record.assigned_faculty_id)
        record.faculty_name = reviewer.name if reviewer else None
    else:
        record.faculty_name = None
    return render_template(
        'hod/request_detail.html',
        user=current_user(),
        request=record,
        proofs=ProofModel.find_by_request(request_id),
        history=ApprovalModel.find_by_request(request_id),
    )


@hod_bp.route('/requests/<int:request_id>/action', methods=['POST'])
@permission_required('hod.request_action')
def request_action(request_id: int):
    """The HOD's decision on a request the lecturer has recommended.

    This is the step that actually grants a permission. The service refuses
    anything not already in AWAITING_HOD, so a request still sitting with a
    lecturer cannot be approved from here -- the two stages stay in order.

    `member_ids` is only meaningful on a group: it is the subset being kept, and
    the service refuses an approval that would leave nobody covered.
    """
    user = current_user()
    action = _approval_action(request.form.get('action', ''))
    remarks = (request.form.get('remarks') or '').strip()
    # `member_scope` disambiguates an empty `member_ids`: unchecked boxes are
    # absent from a POST, so "no ids" means either "solo, no checkboxes" or "the
    # HOD unticked all four". Only the latter must reach the service to be
    # refused, because the alternative is approving a group nobody was left on.
    group_scope = (request.form.get('member_scope') == 'group')
    submitted_members = request.form.getlist('member_ids') if group_scope else None

    try:
        hod_act_on_request(
            request_id=request_id,
            hod=user,
            action=action,
            remarks=remarks,
            base_url=url_for('hod.dashboard', _external=True).rsplit('/', 1)[0],
            member_ids=submitted_members,
        )
    except ValidationError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('hod.requests'))

    verb = 'approved' if action == ApprovalAction.APPROVED else 'rejected'
    record = PermissionModel.find_by_id(request_id)
    if action == ApprovalAction.APPROVED and record and record.is_group:
        kept = len([m for m in record.approved_members])
        flash(
            f'Request #{request_id} approved for {kept} of '
            f'{record.group_size} student{"s" if record.group_size != 1 else ""}.',
            'success',
        )
    else:
        flash(f'Request #{request_id} {verb}.', 'success')
    return redirect(url_for('hod.requests'))


@hod_bp.route('/students')
@permission_required('hod.students')
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
@permission_required('hod.faculty_workload')
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

    routing = SettingsModel.get_routing()

    return render_template(
        'hod/faculty.html', user=user, lecturers=lecturers, routing=routing
    )


@hod_bp.route('/faculty/import', methods=['POST'])
@permission_required('hod.import_faculty')
def import_faculty():
    """Create lecturer accounts from the HOD's Excel sheet.

    The sheet carries Name and Email columns; every valid row becomes a
    LECTURER account (or is reported as already existing), and malformed rows
    are reported rather than aborting the import. Accounts are provisioned
    inactive-sign-in-safe: they sign in through Entra ID like everyone else,
    matching on email at first sign-in.
    """
    upload = request.files.get('faculty_sheet')
    if upload is None or not (upload.filename or '').strip():
        flash('Choose an Excel workbook or CSV file to import.', 'danger')
        return redirect(url_for('hod.faculty_workload'))

    try:
        parsed = parse_faculty_roster(upload)
    except RosterError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('hod.faculty_workload'))

    created, existing = 0, 0
    for entry in parsed['entries'][:500]:
        account = UserModel.find_by_email(entry['email'])
        if account is not None:
            existing += 1
            continue
        UserModel.create(
            None, entry['email'], entry['name'],
            role=UserRole.LECTURER, department='CSE',
        )
        created += 1

    parts = []
    if created:
        parts.append(f'{created} lecturer account{"s" if created != 1 else ""} created')
    if existing:
        parts.append(f'{existing} already existed')
    flash(
        ('Faculty import: ' + ', '.join(parts) + '.')
        if parts else 'Faculty import: no new rows to add.',
        'success' if created else 'warning',
    )
    for error in parsed['errors'][:8]:
        flash(error, 'warning')
    if len(parsed['errors']) > 8:
        flash(f'... and {len(parsed["errors"]) - 8} more skipped rows.',
              'warning')
    return redirect(url_for('hod.faculty_workload'))


@hod_bp.route('/faculty/routing', methods=['POST'])
@permission_required('hod.routing')
def routing():
    """Name the dedicated reviewers for the event, curricular and general queues.

    Each select holds a lecturer id, or nothing to clear that queue back to the
    default (the student's own proctor, then the least-loaded lecturer). Only
    active lecturers are accepted, so a stale or tampered id cannot park a
    queue on an account that cannot review.
    """
    chosen = {}
    for field in ('event_faculty_id', 'curricular_faculty_id',
                  'general_faculty_id'):
        raw = (request.form.get(field) or '').strip()
        lecturer = UserModel.find_by_id(int(raw)) if raw.isdigit() else None
        if raw and (lecturer is None or not lecturer.is_active
                    or not lecturer.is_lecturer):
            flash('One of the selected reviewers is not an active lecturer. '
                  'Nothing was changed.', 'danger')
            return redirect(url_for('hod.faculty_workload'))
        chosen[field] = lecturer.id if lecturer else None

    SettingsModel.set_routing(**chosen)
    flash('Request routing updated.', 'success')
    return redirect(url_for('hod.faculty_workload'))


@hod_bp.route('/classes')
@permission_required('hod.classes')
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
            'kind': row.get('kind') or 'CLASS',
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
@permission_required('hod.reports')
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
                      search, limit=300, faculty_id=None):
    from app.faculty.routes import _filtered_requests as shared
    return shared(status_filter, type_filter, date_from, date_to, search,
                  limit, faculty_id=faculty_id)


@hod_bp.route('/report/print')
@permission_required('hod.print_report')
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