import io
import mimetypes

from contextlib import contextmanager
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.models import (
    ApprovalAction, PermissionType, RequestStatus, UserRole,
)
from app.models.classes import (
    AttendanceModel, ClassModel, MemberModel, KIND_CLASS, KIND_PROCTOR,
)
from app.models.permission import (
    ApprovalModel, PermissionModel, ProofModel, attach_members,
    classify_route,
)
from app.models.user import UserModel
from app.permissions.pages import permission_required
from app.permissions.service import ValidationError, act_on_request, student_can_view
from app.utils.files import UploadError, fetch_proof
from app.utils.qr import letter_qr
from app.utils.roster import RosterError, parse_roster
from app.utils.security import current_user, csrf_token


def _parse_date(value, default):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return default


def annotate_routes(requests):
    """Stamp the review queue each request arrived through.

    New rows carry it; older rows are classified from the reason text, so the
    register reads the same value for both.
    """
    for record in requests:
        record.route_display = (
            getattr(record, 'route_category', None)
            or classify_route(getattr(record, 'reason', ''))
        )
    return requests


faculty_bp = Blueprint('faculty', __name__, url_prefix='/faculty')

APP_ROOT = 'https://cse-permission.adityauniversity.in'


@faculty_bp.route('/dashboard')
@permission_required('faculty.dashboard')
def dashboard():
    user = current_user()
    pending = PermissionModel.find_pending_for_faculty(user.id)
    attach_members(pending)
    annotate_routes(pending)
    history = PermissionModel.find_all_for_hod(limit=50)
    annotate_routes(history)

    return render_template(
        'faculty/dashboard.html',
        user=user,
        pending=pending,
        recent=history,
        pending_count=len(pending),
    )


@faculty_bp.route('/requests/<int:request_id>')
@permission_required('faculty.request_detail')
def request_detail(request_id: int):
    user = current_user()
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        abort(404)

    if user.is_lecturer and record.assigned_faculty_id != user.id:
        abort(403)

    # The group list on this page reads member_details; without it a group
    # renders as a solo request and the reviewer decides half-blind.
    attach_members([record])
    annotate_routes([record])
    student = UserModel.find_by_id(record.student_id)
    proofs = ProofModel.find_by_request(request_id)
    history = ApprovalModel.find_by_request(request_id)

    return render_template(
        'faculty/request_detail.html',
        user=user,
        request=record,
        student=student,
        proofs=proofs,
        history=history,
    )


@faculty_bp.route('/requests/<int:request_id>/action', methods=['POST'])
@permission_required('faculty.action')
def action(request_id: int):
    """The lecturer's decision: grant it, forward it, or refuse it.

    Approving grants the permission outright -- the HOD never sees it. A
    lecturer who has verified the request but cannot permit it forwards it
    instead, which is the only path that reaches the HOD. Rejecting ends it.
    Forwarding and rejecting both need a remark, because the HOD (or the
    student) has to know why.
    """
    user = current_user()
    decision = request.form.get('decision', '').lower()
    remarks = (request.form.get('remarks') or '').strip()

    if decision not in ('approve', 'forward', 'reject'):
        flash('Choose approve, forward to HOD, or reject.', 'danger')
        return redirect(url_for('faculty.request_detail', request_id=request_id))

    if decision in ('reject', 'forward') and len(remarks) < 5:
        if decision == 'reject':
            flash('Add a remark so the student understands the rejection.',
                  'danger')
        else:
            flash('Add a remark so the HOD knows why this needs their decision.',
                  'danger')
        return redirect(url_for('faculty.request_detail', request_id=request_id))

    action_enum = {
        'approve': ApprovalAction.APPROVED,
        'forward': ApprovalAction.FORWARDED,
        'reject': ApprovalAction.REJECTED,
    }[decision]

    try:
        act_on_request(
            request_id=request_id,
            faculty=user,
            action=action_enum,
            remarks=remarks,
            base_url=APP_ROOT,
        )
    except ValidationError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('faculty.request_detail', request_id=request_id))

    verbs = {
        'approve': 'approved',
        'forward': 'verified and forwarded to the HOD',
        'reject': 'rejected',
    }
    flash(f'Request #{request_id} {verbs[decision]}.', 'success')
    return redirect(url_for('faculty.dashboard'))


@faculty_bp.route('/requests/<int:request_id>/reassign', methods=['POST'])
@permission_required('faculty.reassign')
def reassign(request_id: int):
    lecturer_id = request.form.get('lecturer_id', type=int)
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        abort(404)
    if lecturer_id and UserModel.find_by_id(lecturer_id):
        PermissionModel.update_status(
            request_id, record.status, faculty_id=lecturer_id
        )
        flash(f'Reassigned to faculty #{lecturer_id}.', 'success')
    return redirect(url_for('hod.dashboard'))


@faculty_bp.route('/search')
@permission_required('faculty.search')
def search():
    """Find a student by roll number or name and list their permissions."""
    user = current_user()
    query = (request.args.get('q') or '').strip()

    results = []
    if len(query) >= 2:
        results = _search_students(user, query)

    return render_template(
        'faculty/search.html',
        user=user,
        query=query,
        results=results,
    )


def _search_students(viewer, query: str) -> list:
    """Return students matching the query, with their permission summary.

    A lecturer sees every student; both roles share the same department view so
    a handover between lecturers still finds the record.
    """
    from app.models.firestore import store

    needle = query.upper()
    students = []
    for row in store.documents('users', role=UserRole.STUDENT.value,
                               is_active=True):
        roll = (row.get('roll_number') or '').upper()
        name = (row.get('name') or '').upper()
        if needle and needle not in roll and needle not in name:
            continue
        students.append(row)

    # Exact roll first, then roll prefixes, then the rest by roll number. This
    # was a CASE expression in SQL; it is spelled out here because Firestore
    # cannot order by an expression.
    def rank(row):
        roll = (row.get('roll_number') or '').upper()
        if not needle:
            return (0, roll)
        if roll == needle:
            return (0, roll)
        if roll.startswith(needle):
            return (1, roll)
        return (2, roll)

    students.sort(key=rank)

    output = []
    for row in students[:40]:
        roll = (row.get('roll_number') or '').upper()
        record = PermissionModel.find_by_student(row['id'], limit=25)
        output.append({
            'id': row['id'],
            'name': row.get('name'),
            'roll_number': roll,
            'email': row.get('email'),
            'phone': row.get('phone'),
            'total': len(record),
            'approved': sum(1 for r in record if r.status == RequestStatus.APPROVED),
            'pending': sum(1 for r in record if r.status == RequestStatus.PENDING),
            'requests': record,
            'in_my_classes': _class_rolls_for(viewer, roll),
        })
    return output


def _class_rolls_for(viewer, roll_number: str) -> list:
    """Classes of the viewing lecturer that contain this roll number."""
    from app.models.firestore import store

    if not roll_number:
        return []
    class_ids = {row.get('class_id')
                 for row in store.documents('class_members',
                                            roll_number=roll_number)}
    if not class_ids:
        return []
    return [
        {'id': row['id'], 'name': row.get('name')}
        for row in store.documents('class_groups', faculty_id=viewer.id)
        if row['id'] in class_ids
    ]


@faculty_bp.route('/classes')
@permission_required('faculty.classes')
def classes():
    """List the viewing lecturer's taught classes (not their proctor lists)."""
    user = current_user()
    # Pick up any roster rows saved before those students first signed in.
    linked_now = MemberModel.relink_unresolved()

    owned = ClassModel.find_for_faculty(user.id, kind=KIND_CLASS)

    return render_template(
        'faculty/classes.html',
        user=user,
        classes=owned,
        linked_now=linked_now,
    )


@faculty_bp.route('/classes/new', methods=['POST'])
@permission_required('faculty.create_class')
def create_class():
    user = current_user()
    name = (request.form.get('name') or '').strip()

    if len(name) < 3:
        flash('Give the class a name of at least 3 characters.', 'danger')
        return redirect(url_for('faculty.classes'))

    try:
        group = ClassModel.create(
            name=name,
            faculty_id=user.id,
            section_code=(request.form.get('section_code') or '').strip(),
            academic_year=(request.form.get('academic_year') or '').strip(),
        )
    except Exception:
        flash('The class could not be created. Please try again.', 'danger')
        return redirect(url_for('faculty.classes'))

    flash(f'Class "{group.name}" created. Upload a roster to add students.',
          'success')
    return redirect(url_for('faculty.class_detail', class_id=group.id))


@faculty_bp.route('/classes/<int:class_id>')
@permission_required('faculty.class_detail')
def class_detail(class_id: int):
    """Roster, and the permissions in force on a chosen date."""
    user = current_user()
    group = _owned_class(user, class_id)

    view_date = _parse_date(request.args.get('date'), date.today())
    members = MemberModel.find_by_class(class_id)

    permissions_by_student, on_permission = _permissions_for_date(
        members, view_date
    )
    attendance = AttendanceModel.find_by_class_date(class_id, view_date)

    rows = []
    for member in members:
        if member.student_id:
            status = attendance.get(member.student_id)
        else:
            status = None
        records = permissions_by_student.get(member.student_id, [])
        rows.append({
            'member': member,
            'permissions': records,
            'has_permission': bool(records),
            'attendance': status,
        })

    summary = {
        'roster': len(members),
        'linked': sum(1 for m in members if m.is_linked),
        'unlinked': sum(1 for m in members if not m.is_linked),
        'on_permission': sum(1 for r in rows if r['has_permission']),
        'present': sum(1 for r in rows if r['attendance'] == 'PRESENT'),
        'absent': sum(1 for r in rows if r['attendance'] == 'ABSENT'),
    }

    return render_template(
        'faculty/class_detail.html',
        user=user,
        class_group=group,
        members=members,
        rows=rows,
        view_date=view_date,
        summary=summary,
    )


@faculty_bp.route('/classes/<int:class_id>/roster', methods=['POST'])
@permission_required('faculty.upload_roster')
def upload_roster(class_id: int):
    """Bulk-add roll numbers from an Excel or CSV roster."""
    user = current_user()
    _owned_class(user, class_id)

    upload = request.files.get('roster')
    try:
        rolls = parse_roster(upload)
    except RosterError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('faculty.class_detail', class_id=class_id))

    result = MemberModel.add_members(class_id, rolls)

    message = (
        f'Added {result["added"]} roll number'
        f'{"" if result["added"] == 1 else "s"} '
        f'({result["linked"]} matched to student accounts).'
    )
    flash(message, 'success')

    if result['unresolved']:
        preview = ', '.join(result['unresolved'][:6])
        more = '' if len(result['unresolved']) <= 6 else \
            f' and {len(result["unresolved"]) - 6} more'
        flash(
            f'{len(result["unresolved"])} roll numbers have no student account '
            f'yet ({preview}{more}). They will link automatically once those '
            'students sign in.',
            'warning',
        )

    return redirect(url_for('faculty.class_detail', class_id=class_id))


@faculty_bp.route('/classes/<int:class_id>/members/<int:member_id>/delete',
                  methods=['POST'])
@permission_required('faculty.delete_member')
def delete_member(class_id: int, member_id: int):
    user = current_user()
    _owned_class(user, class_id)

    if MemberModel.delete_member(member_id):
        flash('Student removed from the roster.', 'success')
    return redirect(url_for('faculty.class_detail', class_id=class_id))


@faculty_bp.route('/classes/<int:class_id>/delete', methods=['POST'])
@permission_required('faculty.delete_class')
def delete_class(class_id: int):
    user = current_user()
    group = _owned_class(user, class_id)

    if ClassModel.delete(class_id):
        flash(f'Class "{group.name}" deleted.', 'success')
    return redirect(url_for('faculty.classes'))


@faculty_bp.route('/classes/<int:class_id>/attendance', methods=['GET', 'POST'])
@permission_required('faculty.attendance')
def attendance(class_id: int):
    """Mark attendance for the class on a given date."""
    user = current_user()
    _owned_class(user, class_id)

    view_date = _parse_date(request.args.get('date'), date.today())
    members = MemberModel.find_by_class(class_id)

    if request.method == 'POST':
        view_date = _parse_date(request.form.get('date'), view_date)
        if view_date > date.today():
            flash('Attendance cannot be marked for a future date.', 'danger')
            return redirect(url_for('faculty.attendance', class_id=class_id,
                                    date=view_date))

        # Pre-mark students excused by an approved permission covering that day.
        _, on_permission = _permissions_for_date(members, view_date)

        records = []
        for member in members:
            if not member.is_linked:
                continue
            chosen = (request.form.get(f'att_{member.id}') or 'PRESENT').upper()
            if chosen not in ('PRESENT', 'ABSENT', 'ON_PERMISSION'):
                chosen = 'PRESENT'
            if member.id in on_permission and chosen == 'PRESENT':
                chosen = 'ON_PERMISSION'
            records.append((member.student_id, chosen))

        if records:
            saved = AttendanceModel.save(class_id, records, user.id, view_date)
            flash(f'Attendance saved for {saved} student'
                  f'{"" if saved == 1 else "s"} on '
                  f'{view_date.strftime("%d %B %Y")}.', 'success')
        else:
            flash('No students with a linked account to mark.', 'warning')

        return redirect(url_for('faculty.attendance', class_id=class_id,
                                date=view_date))

    existing = AttendanceModel.find_by_class_date(class_id, view_date)
    _, on_permission = _permissions_for_date(members, view_date)

    summary = AttendanceModel.summary_by_date(class_id, view_date)

    return render_template(
        'faculty/attendance.html',
        user=user,
        class_group=_owned_class(user, class_id),
        members=members,
        existing=existing,
        on_permission=on_permission,
        view_date=view_date,
        summary=summary,
    )


# ---------- helpers ----------

def _owned_class(user, class_id: int):
    """Fetch a class, guaranteeing the viewer owns it (admins may read any)."""
    return _owned_group(user, class_id, KIND_CLASS)


def _owned_group(user, group_id: int, kind: str = None):
    """Fetch a group, guaranteeing the viewer owns it (admins may read any).

    `kind` additionally pins the page to the right list: a proctor URL must
    not open a taught class and vice versa, or the two lists the faculty keeps
    would silently mix.
    """
    group = ClassModel.find_by_id(group_id)
    if group is None:
        abort(404)
    if not user.is_hod and group.faculty_id != user.id:
        abort(403)
    if kind and (group.kind or KIND_CLASS) != kind:
        abort(404)
    return group


@faculty_bp.route('/proctor')
@permission_required('faculty.proctor_students')
def proctor_students():
    """The lecturer's proctor lists: rolls whose requests they review.

    Separate from the classes they teach. A class has attendance and
    timetables; a proctor list only decides whose permission requests land in
    this queue.
    """
    user = current_user()
    linked_now = MemberModel.relink_unresolved()

    groups = ClassModel.find_for_faculty(user.id, kind=KIND_PROCTOR)

    return render_template(
        'faculty/proctor.html',
        user=user,
        groups=groups,
        linked_now=linked_now,
    )


@faculty_bp.route('/proctor/new', methods=['POST'])
@permission_required('faculty.create_proctor_group')
def create_proctor_group():
    user = current_user()
    name = (request.form.get('name') or '').strip()

    if len(name) < 3:
        flash('Give the proctor list a name of at least 3 characters.', 'danger')
        return redirect(url_for('faculty.proctor_students'))

    try:
        group = ClassModel.create(
            name=name,
            faculty_id=user.id,
            section_code=(request.form.get('section_code') or '').strip(),
            academic_year=(request.form.get('academic_year') or '').strip(),
            kind=KIND_PROCTOR,
        )
    except Exception:
        flash('The proctor list could not be created. Please try again.', 'danger')
        return redirect(url_for('faculty.proctor_students'))

    flash(f'Proctor list "{group.name}" created. Upload a roster to add students.',
          'success')
    return redirect(url_for('faculty.proctor_group_detail', group_id=group.id))


@faculty_bp.route('/proctor/<int:group_id>')
@permission_required('faculty.proctor_group_detail')
def proctor_group_detail(group_id: int):
    """One proctor list and the rolls on it."""
    user = current_user()
    group = _owned_group(user, group_id, KIND_PROCTOR)

    members = MemberModel.find_by_class(group_id)
    summary = {
        'roster': len(members),
        'linked': sum(1 for m in members if m.is_linked),
        'unlinked': sum(1 for m in members if not m.is_linked),
    }

    return render_template(
        'faculty/proctor_detail.html',
        user=user,
        group=group,
        members=members,
        summary=summary,
    )


@faculty_bp.route('/proctor/<int:group_id>/roster', methods=['POST'])
@permission_required('faculty.upload_proctor_roster')
def upload_proctor_roster(group_id: int):
    """Bulk-add roll numbers to a proctor list from an Excel or CSV roster."""
    user = current_user()
    _owned_group(user, group_id, KIND_PROCTOR)

    upload = request.files.get('roster')
    try:
        rolls = parse_roster(upload)
    except RosterError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('faculty.proctor_group_detail', group_id=group_id))

    result = MemberModel.add_members(group_id, rolls)

    message = (
        f'Added {result["added"]} roll number'
        f'{"" if result["added"] == 1 else "s"} '
        f'({result["linked"]} matched to student accounts).'
    )
    flash(message, 'success')

    if result['unresolved']:
        preview = ', '.join(result['unresolved'][:6])
        more = '' if len(result['unresolved']) <= 6 else \
            f' and {len(result["unresolved"]) - 6} more'
        flash(
            f'{len(result["unresolved"])} roll numbers have no student account '
            f'yet ({preview}{more}). They will link automatically once those '
            'students sign in.',
            'warning',
        )

    return redirect(url_for('faculty.proctor_group_detail', group_id=group_id))


@faculty_bp.route('/proctor/<int:group_id>/members/<int:member_id>/delete',
                  methods=['POST'])
@permission_required('faculty.delete_proctor_member')
def delete_proctor_member(group_id: int, member_id: int):
    user = current_user()
    _owned_group(user, group_id, KIND_PROCTOR)

    if MemberModel.delete_member(member_id):
        flash('Student removed from the proctor list.', 'success')
    return redirect(url_for('faculty.proctor_group_detail', group_id=group_id))


@faculty_bp.route('/proctor/<int:group_id>/delete', methods=['POST'])
@permission_required('faculty.delete_proctor_group')
def delete_proctor_group(group_id: int):
    user = current_user()
    group = _owned_group(user, group_id, KIND_PROCTOR)

    if ClassModel.delete(group_id):
        flash(f'Proctor list "{group.name}" deleted.', 'success')
    return redirect(url_for('faculty.proctor_students'))


def _permissions_for_date(members, on_date: date):
    """Fetch approved permissions overlapping a date, grouped by student id.

    Only students on the roster are considered, and only rows that span the date
    are returned.
    """
    from app.models.firestore import store
    from app.models.permission import attach_students

    rolls = {m.roll_number for m in members if m.is_linked}
    if not rolls:
        return {}, set()

    records = []
    for row in store.documents('permission_requests',
                               status=RequestStatus.APPROVED.value):
        start, end = row.get('start_date'), row.get('end_date')
        if start and end and start <= on_date <= end:
            records.append(PermissionModel._to_request(row))
    attach_students(records)
    records = [r for r in records if (r.student_roll_number or '').upper() in rolls]
    records.sort(key=lambda r: (r.student_roll_number or '',
                                r.start_date or date.min))

    by_student = {}
    excused_member_ids = set()
    roll_to_member = {m.roll_number: m.id for m in members}

    for record in records:
        by_student.setdefault(record.student_id, []).append(record)
        member_id = roll_to_member.get(
            (record.student_roll_number or '').upper())
        if member_id:
            excused_member_ids.add(member_id)

    return by_student, excused_member_ids



@faculty_bp.route('/requests')
@permission_required('faculty.requests_browser')
def requests_browser():
    """Browse every request in the department with filters."""
    user = current_user()

    status_filter = _status_filter(request.args.get('status'))
    type_filter = _type_filter(request.args.get('type'))
    date_from = _parse_date(request.args.get('from'), None)
    date_to = _parse_date(request.args.get('to'), None)
    search = (request.args.get('q') or '').strip()

    records = _filtered_requests(status_filter, type_filter,
                                 date_from, date_to, search, limit=300)

    return render_template(
        'faculty/requests.html',
        user=user,
        requests=records,
        status_filter=status_filter,
        type_filter=type_filter,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )


@faculty_bp.route('/attendance')
@permission_required('faculty.attendance_overview')
def attendance_overview():
    """Pick a class and a date to mark attendance for it."""
    user = current_user()
    classes = ClassModel.find_for_faculty(user.id)

    selected = None
    class_id = request.args.get('class_id', type=int)
    if class_id:
        try:
            selected = _owned_class(user, class_id)
        except Exception:
            selected = None

    if selected is not None:
        return redirect(url_for('faculty.attendance', class_id=selected.id,
                                date=request.args.get('date')))

    return render_template(
        'faculty/attendance_overview.html',
        user=user,
        classes=classes,
        now_date=date.today(),
    )


@faculty_bp.route('/reports')
@permission_required('faculty.reports')
def reports():
    """Workload and trend figures for the signed-in lecturer."""
    user = current_user()
    stats = _faculty_stats(user.id)

    return render_template(
        'faculty/reports.html',
        user=user,
        stats=stats,
    )


# ---------- aggregate helpers ----------

def _faculty_stats(faculty_id: int) -> dict:
    """Decision counts, type split, reason mix and a 7-day trend."""
    from app.models.firestore import store

    requests = store.documents('permission_requests',
                               assigned_faculty_id=faculty_id)
    decisions = store.documents('approval_history', faculty_id=faculty_id)

    stats = {'pending': 0, 'approved': 0, 'rejected': 0,
             'decisions': len(decisions)}
    types = {}
    reasons = {}
    for row in requests:
        status = row.get('status')
        if status in stats:
            stats[status] += 1
        kind = row.get('permission_type')
        types[kind] = types.get(kind, 0) + 1
        reason = row.get('reason')
        reasons[reason] = reasons.get(reason, 0) + 1

    stats['total'] = stats['approved'] + stats['rejected'] + stats['pending']
    # Only the issued type is counted. `leave` is deliberately not reported: the
    # retired type still exists in storage, and surfacing a count for it on a
    # page nobody can act on invites the question of what it means.
    stats['classroom'] = types.get(PermissionType.CLASSROOM.value, 0)
    stats['reasons'] = [
        {'reason': reason, 'n': count}
        for reason, count in sorted(reasons.items(),
                                    key=lambda item: (-item[1], item[0] or ''))[:8]
    ]

    stats['trend'] = _trend_for(faculty_id)
    stats['classes'] = ClassModel.find_for_faculty(faculty_id, kind=KIND_CLASS)
    return stats


def _trend_for(faculty_id: int, days: int = 7) -> dict:
    from app.models.firestore import store

    start = date.today() - timedelta(days=days - 1)

    # Counted per day in Python rather than by casting the timestamp in SQL. The
    # values are already converted to REPORT_TIMEZONE on the way out of the store,
    # so the calendar day here is the same day the old AT TIME ZONE cast produced.
    created_days = {}
    for row in store.documents('permission_requests',
                               assigned_faculty_id=faculty_id):
        day = _report_day(row.get('created_at'))
        if day:
            created_days[day] = created_days.get(day, 0) + 1

    decided_days = {}
    for row in store.documents('approval_history', faculty_id=faculty_id):
        day = _report_day(row.get('actioned_at'))
        if day:
            decided_days[day] = decided_days.get(day, 0) + 1

    labels, created, decided = [], [], []
    for offset in range(days):
        day = start + timedelta(days=offset)
        labels.append(day.strftime('%d %b'))
        created.append(created_days.get(day, 0))
        decided.append(decided_days.get(day, 0))
    return {'labels': labels, 'created': created, 'decided': decided}


def _report_day(value):
    """The calendar day a timestamp belongs to, in REPORT_TIMEZONE."""
    if not isinstance(value, datetime):
        return None
    return value.date()


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
                      search, limit=200, faculty_id=None):
    """Shared filter query used by the faculty browser and HOD request list."""
    from app.models.firestore import store
    from app.models.permission import attach_students

    filters = {}
    # Both are single-field equality filters, which Firestore serves from
    # automatic indexes. Only an ordering would need a composite index, and that
    # is done in Python below.
    if status_filter:
        filters['status'] = status_filter.value
    if faculty_id:
        filters['assigned_faculty_id'] = faculty_id

    requests = [PermissionModel._to_request(row)
                for row in store.documents('permission_requests', **filters)]

    if type_filter:
        requests = [r for r in requests if r.permission_type == type_filter]
    if date_from:
        requests = [r for r in requests if r.end_date and r.end_date >= date_from]
    if date_to:
        requests = [r for r in requests if r.start_date and r.start_date <= date_to]

    attach_students(requests)

    if search:
        needle = search.upper()
        # The search runs over the student's name and roll number, which live on
        # the user rather than the request. A single read of the students involved
        # beats a read per request.
        students = {r.student_id: store.get('users', r.student_id)
                    for r in requests}
        matching = {
            student_id for student_id, row in students.items()
            if row and needle in f'{row.get("roll_number") or ""} {row.get("name") or ""}'.upper()
        }
        requests = [r for r in requests if r.student_id in matching]

    faculty_names = {}
    for record in requests:
        faculty_id_value = record.assigned_faculty_id
        if faculty_id_value and faculty_id_value not in faculty_names:
            row = store.get('users', faculty_id_value)
            faculty_names[faculty_id_value] = row.get('name') if row else None
        record.faculty_name = faculty_names.get(faculty_id_value)
        # Rows written before routing existed carry no category; classifying
        # from the reason keeps the register one value for old and new alike.
        record.route_display = (
            getattr(record, 'route_category', None)
            or classify_route(getattr(record, 'reason', ''))
        )

    requests.sort(key=lambda r: (r.created_at is None,
                                 r.created_at.isoformat() if hasattr(r.created_at, 'isoformat') else '',
                                 -int(r.id or 0)))
    return requests[:int(limit)]


@faculty_bp.route('/requests/<int:request_id>/letter')
@permission_required('faculty.request_letter')
def request_letter(request_id: int):
    """Formal letter for a request, addressed from the faculty portal."""
    user = current_user()
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        abort(404)

    if user.is_lecturer and record.assigned_faculty_id != user.id:
        abort(403)

    from flask import render_template

    student = UserModel.find_by_id(record.student_id)
    attach_members([record])
    history = ApprovalModel.find_by_request(request_id)
    faculty_name = '—'
    decision = None
    if history:
        latest = history[-1]
        faculty_name = getattr(latest, 'faculty_name', None) or '—'
        decision = latest

    # The QR points at the same public verification page the student's copy does.
    # This view renders the same template as the student route, and the template
    # draws the code only when it is handed one -- so a letter printed from here
    # used to arrive with no code at all, and the reference number in the table
    # still read correctly from its fallback, which is what made it easy to miss.
    # A failing encoder degrades to a letter without a code rather than to a
    # letter that will not print.
    try:
        qr = letter_qr(record.id)
    except Exception:
        current_app.logger.exception(
            'Could not build the verification QR code'
        )
        qr = {'url': '', 'image': '', 'reference': f'REQ-{record.id:04d}'}

    return render_template(
        'student/letter.html',
        user=user,
        request=record,
        student=student,
        proofs=ProofModel.find_by_request(request_id),
        history=history,
        faculty_name=faculty_name,
        decision=decision,
        qr=qr,
    )


@faculty_bp.route('/proofs/<int:proof_id>/download')
@permission_required('faculty.download_proof')
def download_proof(proof_id: int):
    proof = ProofModel.find_by_id(proof_id)
    if proof is None:
        abort(404)

    record = PermissionModel.find_by_id(proof.request_id)
    if record is None:
        abort(404)

    user = current_user()
    if user.is_student and not student_can_view(record, user):
        abort(403)
    if user.is_lecturer and record.assigned_faculty_id != user.id:
        abort(403)

    try:
        payload = fetch_proof(proof.file_path)
    except UploadError:
        abort(404)

    mimetype, _ = mimetypes.guess_type(proof.original_filename)
    response = send_file(
        io.BytesIO(payload),
        mimetype=mimetype or f'application/{proof.file_type}',
        as_attachment=False,
        download_name=proof.original_filename,
    )
    # The proof is previewed in an iframe on the request page, and the blanket
    # X-Frame-Options: DENY every response carries made the browser refuse to
    # render it. SAMEORIGIN is the narrowest relaxation that fixes that: the
    # document can be framed by our own pages and still cannot be framed by
    # anyone else, which is what DENY was there to prevent. The header is
    # overwritten here rather than cleared, because setdefault in the
    # after_request hook has already put DENY on this response.
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response
