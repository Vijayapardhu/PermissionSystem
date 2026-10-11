from collections import Counter
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)

from app.models import (
    ApprovalAction, LeaveStatus, PermissionType, RequestStatus, UserRole,
)
from app.models.classes import ClassModel, MemberModel, KIND_CLASS, KIND_PROCTOR
from app.models.leave import (
    AdjustmentModel, LeaveModel, LeaveSummary, TimetableModel,
)
from app.models.permission import (
    ApprovalModel, PermissionModel, ProofModel, attach_members,
)
from app.models.routes import RouteModel
from app.models.settings import SettingsModel
from app.models.user import UserModel
from app.permissions.leave_service import (
    LeaveError, hod_act_on_leave, letter_context,
)
from app.permissions.pages import permission_required
from app.permissions.service import (
    ValidationError, categorize_reason, hod_act_directly,
    hod_act_on_request,
)
from app.utils.roster import RosterError, parse_faculty_roster
from app.utils.changes import diff_fields, summarise
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

    # The HOD's to-do first, longest-waiting on top; everything else keeps the
    # register order below it. A register that buries the actionable rows
    # under decided ones is what makes the list feel unreadable.
    from app.faculty.routes import annotate_events as _annotate_events
    _annotate_events(records)
    today = date.today()
    for record in records:
        start, end = record.start_date, record.end_date
        record.day_count = (end - start).days + 1 if start and end else None
        created = record.created_at
        if isinstance(created, datetime):
            created = created.date()
        record.waiting_days = (today - created).days if created else None
    awaiting = [r for r in records
                if r.status == RequestStatus.AWAITING_HOD]
    others = [r for r in records
              if r.status != RequestStatus.AWAITING_HOD]

    reviewers = [
        {'id': lecturer.id, 'name': lecturer.name}
        for lecturer in UserModel.get_faculty()
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
        awaiting=awaiting,
        others=others,
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
    from app.faculty.routes import annotate_events as _annotate_events
    _annotate_events([record])
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


@hod_bp.route('/requests/<int:request_id>/decide', methods=['POST'])
@permission_required('hod.request_action')
def decide_request_directly(request_id: int):
    """The HOD decides a request still sitting in somebody else's queue.

    Routing puts a request in the student's proctor's queue. That proctor may be
    unavailable, and a student waiting on a letter does not get one by waiting
    longer -- so the HOD can take the decision here without waiting for a stage
    that is not moving.

    Same grant as the ordinary HOD action, and deliberately: both write the
    status of a request and both must stay the HOD's alone.
    """
    user = current_user()
    action = _approval_action(request.form.get('action', ''))
    remarks = (request.form.get('remarks') or '').strip()

    group_scope = (request.form.get('member_scope') == 'group')
    submitted_members = request.form.getlist('member_ids') if group_scope else None

    try:
        hod_act_directly(
            request_id=request_id,
            hod=user,
            action=action,
            remarks=remarks,
            base_url=url_for('hod.dashboard', _external=True).rsplit('/', 1)[0],
            member_ids=submitted_members,
        )
    except ValidationError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('hod.request_detail', request_id=request_id))

    verb = 'approved' if action == ApprovalAction.APPROVED else 'rejected'
    flash(f'Request #{request_id} {verb} directly by the HOD.', 'success')
    return redirect(url_for('hod.request_detail', request_id=request_id))


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
    proctor_counts = Counter()
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

    # Taught classes only, and the proctor count kept beside it rather than
    # folded in. The column is labelled "Classes"; a proctor list is not one, so
    # counting it here told the HOD a student was in more classes than they are,
    # and hid the fact that they had no proctor at all.
    proctor_class_ids = {
        row['id'] for row in store.documents('class_groups')
        if (row.get('kind') or KIND_CLASS) == KIND_PROCTOR
    }
    for row in store.documents('class_members'):
        if row.get('class_id') in proctor_class_ids:
            proctor_counts[row.get('student_id')] += 1
        else:
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
            'proctored': proctor_counts[student_id] > 0,
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
    # Taught classes only. A proctor list is a mentoring allocation, and
    # counting it here inflated the workload figure the HOD uses to decide who
    # is carrying what -- a lecturer mentoring two batches looked like they were
    # teaching two more subjects.
    proctor_group_ids = {
        row['id'] for row in store.documents('class_groups')
        if (row.get('kind') or KIND_CLASS) == KIND_PROCTOR
    }
    for row in store.documents('class_groups'):
        if row.get('id') in proctor_group_ids:
            continue
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
            # Carried so the template can offer 'make deputy' to a
            # lecturer and 'revoke' to a deputy, instead of the same
            # button doing both and the HOD having to remember which.
            'role': user_row.get('role'),
        }
        for user_row in store.documents('users',
                                        role=UserRole.LECTURER.value,
                                        is_active=True)
    ]
    lecturers.sort(key=lambda l: (-l['pending'], l['name'] or ''))

    return render_template(
        'hod/faculty.html', user=user, lecturers=lecturers
    )


@hod_bp.route('/faculty/import', methods=['GET', 'POST'])
@permission_required('hod.import_faculty')
def import_faculty():
    """Add lecturers from the HOD's Excel sheet.

    GET shows the import page; POST reads the uploaded sheet and creates a
    LECTURER account per valid row. Accounts sign in through Entra ID like
    everyone else, matching on email at first sign-in.
    """
    if request.method == 'GET':
        return render_template('hod/faculty_import.html', user=current_user())

    upload = request.files.get('faculty_sheet')
    if upload is None or not (upload.filename or '').strip():
        flash('Choose an Excel workbook or CSV file to import.', 'danger')
        return redirect(url_for('hod.import_faculty'))

    try:
        parsed = parse_faculty_roster(upload)
    except RosterError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('hod.import_faculty'))

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
    return redirect(url_for('hod.import_faculty'))


@hod_bp.route('/faculty/add', methods=['POST'])
@permission_required('hod.add_faculty')
def add_faculty():
    """Create one lecturer account from a name and email.

    The same account the spreadsheet import would create, for the common case
    of a single joining faculty. An existing email is reported rather than
    duplicated.
    """
    from app.utils.security import is_valid_email

    name = (request.form.get('name') or '').strip()
    email = (request.form.get('email') or '').strip().lower()

    if len(name) < 2:
        flash('Enter the faculty name.', 'danger')
        return redirect(url_for('hod.import_faculty'))
    if not is_valid_email(email):
        flash(f'"{request.form.get("email") or "—"}" is not a valid email.',
              'danger')
        return redirect(url_for('hod.import_faculty'))
    if UserModel.find_by_email(email) is not None:
        flash(f'{email} already has an account. Nothing was added.',
              'warning')
        return redirect(url_for('hod.import_faculty'))

    UserModel.create(
        None, email, name, role=UserRole.LECTURER, department='CSE',
    )
    flash(f'Lecturer account created for {name} ({email}). '
          f'They can sign in with Microsoft now.', 'success')
    return redirect(url_for('hod.import_faculty'))


@hod_bp.route('/faculty/routing', methods=['GET', 'POST'])
@permission_required('hod.routing')
def routing():
    """Name the dedicated reviewers for the event, curricular and general queues.

    GET shows the routing page, with the reviewer each queue currently points at
    spelled out beside its select. POST does not save: it previews the change,
    and `/faculty/routing/apply` is what writes. Routing is department-wide and
    silently decides whose queue every future request lands in, so it gets the
    same confirmation as everything else.
    """
    current = SettingsModel.get_routing()
    faculty = [{'id': row.id, 'name': row.name}
               for row in UserModel.get_faculty()]

    if request.method == 'GET':
        return render_template(
            'hod/routing.html',
            routes=RouteModel.find_all(),
            user=current_user(),
            lecturers=faculty,
            routing=current,
            current_names=_routing_names(current),
        )

    chosen, error = _routing_choice(request.form)
    if error:
        flash(error, 'danger')
        return render_template(
            'hod/routing.html',
            routes=RouteModel.find_all(),
            user=current_user(),
            lecturers=faculty,
            routing=current,
            current_names=_routing_names(current),
            form=request.form,
        )

    changes = _routing_changes(current, chosen)
    if not changes:
        flash('Nothing was changed.', 'info')
        return redirect(url_for('hod.routing'))

    return render_template('hod/routing_confirm.html', user=current_user(),
                           changes=changes, form=request.form)


@hod_bp.route('/faculty/set-deputy', methods=['POST'])
@permission_required('hod.set_deputy')
def set_deputy():
    """Promote a lecturer to Deputy HOD, or revoke it.

    Promotion is a role change on the account the lecturer already has, so
    nothing about their teaching moves: their classes, their proctor list and
    their leave are all attached to the person rather than to a job title. A
    separate "deputy" account would have split one human in half and taken the
    teaching with them.

    Two things are deliberately not affected. A deputy is not part of
    `is_approver`, so faculty leave still climbs the fixed HOD/Dean ladder -- a
    deputy who could sign their own leave past the HOD would be a hole in the
    workflow that exists to stop exactly that. And a deputy keeps no
    administrative panel: no faculty register, no routing, no events.
    """
    from app.models import UserRole
    from app.models.user import UserModel

    user = current_user()
    faculty_id = request.form.get('faculty_id', type=int)
    make_deputy = request.form.get('make_deputy') == '1'

    person = UserModel.find_by_id(faculty_id) if faculty_id else None
    if person is None:
        flash('Choose a member of teaching staff.', 'danger')
        return redirect(url_for('hod.faculty_workload'))

    # Teaching staff only. A student or a Dean is not a candidate however the
    # form was submitted.
    if not person.is_faculty or not person.is_active:
        flash(f'{person.name} is not active teaching staff.', 'danger')
        return redirect(url_for('hod.faculty_workload'))

    if person.id == user.id:
        # The HOD is above the deputy, not one. Letting the HOD demote
        # themselves would leave the department with no delegated authority at
        # all, without anyone deciding that.
        flash('You are the Head of Department. The deputy role reports to you.',
              'danger')
        return redirect(url_for('hod.faculty_workload'))

    current_deputies = UserModel._by_role(UserRole.DEPUTY_HOD.value, 'name')
    if make_deputy and len(current_deputies) >= 3:
        flash('There are already three Deputy HODs. Revoke one before '
              'appointing another.', 'danger')
        return redirect(url_for('hod.faculty_workload'))

    target = UserRole.DEPUTY_HOD if make_deputy else UserRole.LECTURER
    if person.role == target:
        flash(f'{person.name} is already in that role.', 'info')
        return redirect(url_for('hod.faculty_workload'))

    # `set_role` hands back the re-read account, or None when there is nothing
    # under that id. Tested against None rather than for truthiness: a User
    # object is truthy, so a future empty-but-present row would pass a
    # truthiness check and report a success that never happened.
    updated = UserModel.set_role(person.id, target)
    if updated is None:
        flash('The role could not be changed.', 'danger')
        return redirect(url_for('hod.faculty_workload'))

    if make_deputy:
        flash(f'{person.name} is now a Deputy HOD.', 'success')
    else:
        flash(f'{person.name} is a lecturer again. Their classes are unchanged.',
              'success')
    return redirect(url_for('hod.faculty_workload'))


@hod_bp.route('/faculty/routing/routes/new', methods=['POST'])
@permission_required('hod.apply_routing')
def route_create():
    """Add a review desk."""
    from app.models.routes import RouteModel

    name = (request.form.get('name') or '').strip()
    if len(name) < 2:
        flash('Give the route a name of at least 2 characters.', 'danger')
        return redirect(url_for('hod.routing'))

    # Keywords arrive as one comma-separated field because typing them into
    # separate boxes is how a half-empty rule gets saved and then silently
    # matches nothing.
    keywords = request.form.get('keywords', '')
    RouteModel.create(
        name=name,
        keywords=[k for k in (keywords or '').split(',')],
        faculty_id=request.form.get('faculty_id', type=int),
        position=request.form.get('position', type=int) or 0,
        description=(request.form.get('description') or '').strip(),
    )
    flash(f'Route "{name}" added.', 'success')
    return redirect(url_for('hod.routing'))


@hod_bp.route('/faculty/routing/routes/<int:route_id>/save', methods=['POST'])
@permission_required('hod.apply_routing')
def route_save(route_id: int):
    """Edit a desk: its name, its words, or who reviews it."""
    from app.models.routes import RouteModel

    if RouteModel.find_by_id(route_id) is None:
        abort(404)

    RouteModel.update(
        route_id,
        name=request.form.get('name'),
        keywords=request.form.get('keywords'),
        faculty_id=request.form.get('faculty_id', type=int),
        position=request.form.get('position', type=int),
        description=request.form.get('description'),
    )
    flash('Route updated.', 'success')
    return redirect(url_for('hod.routing'))


@hod_bp.route('/faculty/routing/routes/<int:route_id>/delete', methods=['POST'])
@permission_required('hod.apply_routing')
def route_delete(route_id: int):
    """Remove a desk. Requests already assigned to it are left alone."""
    from app.models.routes import RouteModel

    rule = RouteModel.find_by_id(route_id)
    if rule is None:
        abort(404)
    RouteModel.delete(route_id)
    flash(f'Route "{rule.name}" removed.', 'info')
    return redirect(url_for('hod.routing'))


@hod_bp.route('/faculty/routing/apply', methods=['POST'])
@permission_required('hod.routing')
def apply_routing():
    """Write a confirmed routing change."""
    current = SettingsModel.get_routing()

    chosen, error = _routing_choice(request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('hod.routing'))

    changes = _routing_changes(current, chosen)
    if not changes:
        flash('Nothing was changed.', 'info')
        return redirect(url_for('hod.routing'))

    SettingsModel.set_routing(**chosen)
    flash(f'Request routing updated. {summarise(changes)}', 'success')
    return redirect(url_for('hod.routing'))


ROUTING_FIELDS = (
    ('event_faculty_id', 'Event requests'),
    ('curricular_faculty_id', 'Curricular requests'),
    ('general_faculty_id', 'General proctor requests'),
)

ROUTING_LABELS = dict(ROUTING_FIELDS)


def _routing_names(current):
    """Each queue's reviewer, as a name, resolved at read time."""
    people = {u.id: u.name for u in UserModel.get_faculty()}
    return {field: (people.get(current.get(field)) or 'Falls through to the '
                                                  'default rule')
            for field, _label in ROUTING_FIELDS}


def _routing_choice(form):
    """(chosen, error) from a posted routing form."""
    chosen = {}
    for field, label in ROUTING_FIELDS:
        raw = (form.get(field) or '').strip()
        person = UserModel.find_by_id(int(raw)) if raw.isdigit() else None
        if raw and (person is None or not person.is_active
                    or not person.is_faculty):
            return {}, (f'The reviewer for {label.lower()} is not on the '
                        f'teaching staff. Nothing was changed.')
        chosen[field] = person.id if person else None
    return chosen, None


def _routing_changes(current, chosen):
    """What a routing change would move, named by queue rather than by column."""
    people = {u.id: u.name for u in UserModel.get_faculty()}

    def show(value):
        return people.get(value) or 'Falls through to the default rule'

    return diff_fields(
        {field: show(current.get(field)) for field, _ in ROUTING_FIELDS},
        {field: show(chosen.get(field)) for field, _ in ROUTING_FIELDS},
        ROUTING_LABELS,
    )


@hod_bp.route('/events')
@permission_required('hod.events')
def events():
    """Main events and their sub-events, each with a coordinator.

    One management page for one job: what the department organises and which
    lecturer coordinates each part, so requests naming an event route to the
    right desk.
    """
    from app.models.events import EventModel

    user = current_user()
    tree = EventModel.list_tree()
    lecturers = [
        {'id': row.id, 'name': row.name}
        for row in UserModel.get_faculty()
    ]
    return render_template(
        'hod/events.html', user=user, mains=tree, lecturers=lecturers
    )


@hod_bp.route('/events/<int:event_id>')
@permission_required('hod.event_detail')
def event_detail(event_id: int):
    """One event on its own page: its coordinator, its subs, and its requests.

    The management page edits inline, which is fine for a one-character fix and
    useless for looking an event up. This is the read-only view; editing goes
    through its own page so the change can be previewed first.
    """
    from app.models.events import EventModel
    from app.models.firestore import store

    event = EventModel.find_by_id(event_id)
    if event is None:
        abort(404)

    parent = (EventModel.find_by_id(event.parent_id)
              if event.parent_id else None)
    # A main event's subs come from the tree; a sub event has none of its own.
    subs = []
    for main in EventModel.list_tree():
        if main.id == event_id:
            subs = list(getattr(main, 'subs', []))

    coordinator = (UserModel.find_by_id(event.coordinator_id)
                   if event.coordinator_id else None)
    requests = [r for r in store.documents('permission_requests')
                if r.get('event_id') == event_id]

    return render_template(
        'hod/event_detail.html',
        user=current_user(),
        event=event,
        parent=parent,
        subs=subs,
        coordinator=coordinator,
        request_count=len(requests),
        faculty=UserModel.get_faculty(),
    )


@hod_bp.route('/events/<int:event_id>/edit', methods=['GET', 'POST'])
@permission_required('hod.edit_event')
def edit_event(event_id: int):
    """Rename an event or move its coordinator, previewing the change first.

    The parent is not editable, for the reason the inline editor already gave:
    re-parenting would rewrite what past requests that named the sub-event meant.
    """
    from app.models.events import EventModel

    event = EventModel.find_by_id(event_id)
    if event is None:
        abort(404)

    faculty = [{'id': u.id, 'name': u.name} for u in UserModel.get_faculty()]
    parent = (EventModel.find_by_id(event.parent_id)
              if event.parent_id else None)
    coordinator = (UserModel.find_by_id(event.coordinator_id)
                   if event.coordinator_id else None)
    context = {
        'event': event,
        'faculty': faculty,
        'parent_name': parent.name if parent else None,
        'current_coordinator': coordinator.name if coordinator else 'Nobody assigned',
    }

    if request.method == 'GET':
        return render_template('hod/event_edit.html', user=current_user(),
                               **context)

    changes, error = _event_changes(event, request.form)
    if error:
        flash(error, 'danger')
        return render_template('hod/event_edit.html', user=current_user(),
                               form=request.form, **context)

    if not changes:
        flash('Nothing was changed.', 'info')
        return redirect(url_for('hod.event_detail', event_id=event_id))

    return render_template('hod/event_edit_confirm.html', user=current_user(),
                           event=event, changes=changes, form=request.form)


@hod_bp.route('/events/<int:event_id>/edit/apply', methods=['POST'])
@permission_required('hod.edit_event')
def apply_event_edit(event_id: int):
    """Write a confirmed event edit."""
    from app.models.events import EventModel

    event = EventModel.find_by_id(event_id)
    if event is None:
        abort(404)

    changes, error = _event_changes(event, request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('hod.edit_event', event_id=event_id))
    if not changes:
        flash('Nothing was changed.', 'info')
        return redirect(url_for('hod.event_detail', event_id=event_id))

    raw = (request.form.get('coordinator_id') or '').strip()
    EventModel.update(event_id, request.form.get('name', ''),
                      UserModel.find_by_id(int(raw)).id if raw.isdigit() else None)
    flash(f'{summarise(changes)} Saved.', 'success')
    return redirect(url_for('hod.event_detail', event_id=event_id))


EVENT_EDIT_LABELS = {'name': 'Event name', 'coordinator_id': 'Coordinator'}


def _event_changes(event, form):
    """(changes, error) for a proposed event edit."""
    name = (form.get('name') or '').strip()
    if len(name) < 3:
        return [], 'Give the event a name of at least 3 characters.'
    raw = (form.get('coordinator_id') or '').strip()
    if raw and not raw.isdigit():
        return [], 'Choose a coordinator from the list.'
    coordinator = UserModel.find_by_id(int(raw)) if raw.isdigit() else None
    if raw and (coordinator is None or not coordinator.is_active
                or not coordinator.is_faculty):
        return [], 'The chosen coordinator is not on the teaching staff.'
    current = UserModel.find_by_id(event.coordinator_id) if event.coordinator_id \
        else None
    return diff_fields(
        {'name': event.name,
         'coordinator_id': current.name if current else ''},
        {'name': name,
         'coordinator_id': coordinator.name if coordinator else ''},
        EVENT_EDIT_LABELS,
    ), None


@hod_bp.route('/events/save', methods=['POST'])
@permission_required('hod.save_event')
def save_event():
    """Create a main or sub-event, or rename/reassign an existing one.

    One endpoint for every write: `event_id` empty means create (with an
    optional `parent_id` for a sub-event), otherwise update. Parents never
    change after creation -- moving a sub-event between mains would rewrite
    what past requests meant.
    """
    from app.models.events import EventModel

    raw_id = (request.form.get('event_id') or '').strip()
    name = (request.form.get('name') or '').strip()
    raw_parent = (request.form.get('parent_id') or '').strip()
    raw_coordinator = (request.form.get('coordinator_id') or '').strip()

    if len(name) < 3:
        flash('Give the event a name of at least 3 characters.', 'danger')
        return redirect(url_for('hod.events'))

    coordinator = (UserModel.find_by_id(int(raw_coordinator))
                   if raw_coordinator.isdigit() else None)
    if raw_coordinator and (coordinator is None or not coordinator.is_active
                            or not coordinator.is_faculty):
        flash('The chosen coordinator is not on the teaching staff. '
              'Nothing was saved.', 'danger')
        return redirect(url_for('hod.events'))

    if raw_id:
        event = (EventModel.find_by_id(int(raw_id))
                 if raw_id.isdigit() else None)
        if event is None:
            flash('That event no longer exists.', 'danger')
            return redirect(url_for('hod.events'))
        EventModel.update(event.id, name,
                          coordinator.id if coordinator else None)
        flash(f'Event "{name}" updated.', 'success')
        return redirect(url_for('hod.events'))

    parent = (EventModel.find_by_id(int(raw_parent))
              if raw_parent.isdigit() else None)
    if raw_parent and parent is None:
        flash('The chosen main event no longer exists.', 'danger')
        return redirect(url_for('hod.events'))
    if parent is not None and not parent.is_main:
        flash('A sub-event cannot have children of its own. Pick a main '
              'event as the parent.', 'danger')
        return redirect(url_for('hod.events'))

    EventModel.create(name, coordinator.id if coordinator else None,
                      parent.id if parent else None)
    flash(f'Event "{name}" created'
          f'{" under " + parent.name if parent else ""}.', 'success')
    return redirect(url_for('hod.events'))


@hod_bp.route('/events/delete', methods=['POST'])
@permission_required('hod.delete_event')
def delete_event():
    """Delete an event and its sub-events. Past requests keep their snapshot."""
    from app.models.events import EventModel

    raw_id = (request.form.get('event_id') or '').strip()
    event = (EventModel.find_by_id(int(raw_id))
             if raw_id.isdigit() else None)
    if event is None:
        flash('That event no longer exists.', 'danger')
        return redirect(url_for('hod.events'))

    removed = EventModel.delete(event.id)
    subs = removed - 1
    flash(f'Event "{event.name}" deleted'
          f'{f" with {subs} sub-event{'s' if subs != 1 else ''}" if subs else ""}.',
          'success')
    return redirect(url_for('hod.events'))


# What the class search box matches against. The lecturer modes exist because
# the department refers to one person both ways -- "RAPAKA RAHUL" on a letter and
# "6848" on a roster -- and a single combined box would only ever find whichever
# one the searcher happened to type.
CLASS_SEARCH_MODES = (
    ('class', 'Class name'),
    ('section', 'Timetable / section'),
    ('subject', 'Subject'),
    ('faculty', 'Lecturer name'),
    ('code', 'Employee code'),
)

# What the box invites you to type. The mode dropdown is the one control whose
# effect cannot be guessed from what is already on screen, so the placeholder
# carries the meaning instead of a label above it.
CLASS_SEARCH_PLACEHOLDERS = {
    'class': 'Search class names…',
    'section': 'Search timetables, e.g. T1-General…',
    'subject': 'Search subjects, e.g. ADSAA…',
    'faculty': 'Search lecturer names…',
    'code': 'Search employee codes, e.g. 6113…',
}

_VALID_SEARCH_MODES = {value for value, _label in CLASS_SEARCH_MODES}


def _class_search_mode(raw):
    """The search mode, defaulting to the class name rather than to nothing."""
    raw = (raw or '').strip()
    return raw if raw in _VALID_SEARCH_MODES else 'class'


def _matches_search(text, needle):
    """Case-insensitive contains, tolerant of how the needle is spaced.

    Both sides are collapsed to runs of single spaces. The stored names are
    already normalised on import, so this is really about the other side: a
    searcher who types "RAPAKA  RAHUL" or " Rapaka Rahul " means the same
    person and should not be told the list is empty.
    """
    haystack = ' '.join((text or '').split()).casefold()
    return ' '.join(needle.split()).casefold() in haystack


@hod_bp.route('/classes')
@permission_required('hod.classes')
def classes():
    """Every class across all lecturers, with filters.

    Filters are read from the query string rather than posted, so a narrowed
    view is a shareable link and the browser's back button walks back through
    the filters rather than out of the page.
    """
    from app.models.firestore import store

    user = current_user()
    MemberModel.relink_unresolved()

    section_filter = (request.args.get('section') or '').strip()
    subject_filter = (request.args.get('subject') or '').strip()
    # -1 rather than None: a filter of "0" must be distinguishable from unset.
    lecturer_filter = request.args.get('lecturer', type=int)
    search = (request.args.get('q') or '').strip()
    mode = _class_search_mode(request.args.get('q_type'))

    staff = {
        row['id']: row
        for row in store.documents('users')
        if row.get('role') in ('LECTURER', 'HOD', 'DEPUTY_HOD')
    }
    member_counts = Counter()
    linked_counts = Counter()
    for row in store.documents('class_members'):
        member_counts[row.get('class_id')] += 1
        if row.get('student_id'):
            linked_counts[row.get('class_id')] += 1

    total = 0
    groups = []
    sections = set()
    subjects = set()
    lecturers = {}
    # Taught classes only. This register is about teaching; a proctor list has
    # no subject and no section, so listing it here produced rows that could not
    # be filtered by either. Proctor allocation is visible from the faculty
    # workload and from the "no proctor" mark on the student register.
    for row in store.documents('class_groups'):
        if (row.get('kind') or KIND_CLASS) != KIND_CLASS:
            continue
        total += 1
        owner = staff.get(row.get('faculty_id'))
        section = row.get('section_code')
        subject = row.get('subject_code')
        if section:
            sections.add(section)
        if subject:
            subjects.add(subject)
        if owner:
            lecturers[owner['id']] = owner

        groups.append({
            'id': row['id'],
            'name': row.get('name'),
            'kind': row.get('kind') or 'CLASS',
            'section_code': section,
            'subject_code': subject,
            'academic_year': row.get('academic_year'),
            'faculty_id': row.get('faculty_id'),
            'created_at': row.get('created_at'),
            'faculty_name': (owner or {}).get('name'),
            'faculty_code': (owner or {}).get('employee_code'),
            'member_count': member_counts[row['id']],
            'linked_count': linked_counts[row['id']],
        })

    filtered = [
        g for g in groups
        if (not section_filter or g['section_code'] == section_filter)
        and (not subject_filter or g['subject_code'] == subject_filter)
        and (lecturer_filter is None or g['faculty_id'] == lecturer_filter)
        and (not search or _class_search_hit(g, search, mode))
    ]

    # Lecturer name ascending, then newest class first -- the order the query
    # used to state. Sorted twice on purpose: Python's sort is stable, so the
    # first pass fixes the recency and the second keeps it inside each lecturer.
    filtered.sort(key=lambda g: _sort_key(g['created_at']), reverse=True)
    filtered.sort(key=lambda g: g['faculty_name'] or '')

    active = {
        'section': section_filter,
        'subject': subject_filter,
        'lecturer': lecturer_filter,
        'q': search,
    }
    labels = {
        'section': ('Timetable', section_filter),
        'subject': ('Subject', subject_filter),
        'lecturer': ('Lecturer', (lecturers.get(lecturer_filter) or {}).get('name')
                     if lecturer_filter is not None else None),
        'q': (_mode_label(mode), search),
    }
    chips = [
        {'key': key, 'label': labels[key][0], 'value': labels[key][1],
         'remove_url': _classes_url_without(active, key)}
        for key in ('section', 'subject', 'lecturer', 'q')
        if active.get(key) not in ('', None)
    ]

    return render_template(
        'hod/classes.html',
        user=user,
        classes=filtered,
        total=total,
        sections=sorted(sections),
        subjects=sorted(subjects),
        # Only lecturers who actually own a class. Listing all 214 and having
        # 150 of them return nothing is a dropdown that wastes the scroll.
        lecturers=sorted(lecturers.values(),
                         key=lambda u: (u.get('name') or '').casefold()),
        section_filter=section_filter,
        subject_filter=subject_filter,
        lecturer_filter=lecturer_filter,
        search=search,
        search_mode=mode,
        search_mode_label=_mode_label(mode),
        search_placeholder=CLASS_SEARCH_PLACEHOLDERS.get(
            mode, CLASS_SEARCH_PLACEHOLDERS['class']),
        search_modes=CLASS_SEARCH_MODES,
        # Same page, same dropdowns, no query -- for the x inside the box.
        clear_search_url=_classes_url_without(active, 'q'),
        chips=chips,
        active_filters=bool(chips),
    )


def _mode_label(mode):
    return dict(CLASS_SEARCH_MODES).get(mode, 'Search')


def _classes_url_without(active, drop):
    """This page's URL with one filter removed and the rest kept.

    Chips that reset everything else are worse than useless -- dropping three
    filters to undo one is how a searcher loses the narrowing they built up.
    """
    keep = {k: v for k, v in active.items()
            if k != drop and v not in ('', None)}
    if 'q' not in keep:
        # The search mode is only meaningful alongside a query, and carrying it
        # alone would silently switch the box back to whatever mode it was left
        # in once a new search is typed.
        keep.pop('q_type', None)
    return url_for('hod.classes', **keep) if keep else url_for('hod.classes')


def _class_search_hit(group, needle, mode):
    """Whether one class matches the search box, under the chosen mode."""
    if mode == 'section':
        return _matches_search(group.get('section_code'), needle)
    if mode == 'subject':
        return _matches_search(group.get('subject_code'), needle)
    if mode == 'faculty':
        return _matches_search(group.get('faculty_name'), needle)
    if mode == 'code':
        # An employee code is a number people read off a roster and retype, so
        # "848" has to find 6848 rather than nothing.
        code = (group.get('faculty_code') or '').strip()
        return bool(code) and needle in code
    # 'class' also matches the subject and section, because the class name is
    # built out of both and people search for the parts.
    return (_matches_search(group.get('name'), needle)
            or _matches_search(group.get('subject_code'), needle)
            or _matches_search(group.get('section_code'), needle))


@hod_bp.route('/classes/<int:class_id>')
@permission_required('hod.class_detail')
def class_detail(class_id: int):
    """One class group in full: roster and the permissions in force on a date.

    Read-only, unlike the faculty's own class page: rosters are theirs to keep.
    The HOD opens this to see who is on permission, not to edit.
    """
    from app.faculty.routes import _permissions_for_date as _for_date

    group = ClassModel.find_by_id(class_id)
    if group is None:
        abort(404)
    # A proctor list is not a class. Opening one here rendered a roster and a
    # permission summary as though it were a section, which is the same confusion
    # this page was fixed for everywhere else. The HOD does not need a detail
    # view of proctoring: the lecturer owns that list, and the HOD sees coverage
    # from the student register.
    if (group.kind or KIND_CLASS) != KIND_CLASS:
        abort(404)

    view_date = _parse_date(request.args.get('date'), date.today())
    members = MemberModel.find_by_class(class_id)

    permissions_by_student, _ = _for_date(members, view_date)

    rows = []
    for member in members:
        records = permissions_by_student.get(member.student_id, [])
        rows.append({
            'member': member,
            'permissions': records,
            'has_permission': bool(records),
        })

    owner = UserModel.find_by_id(group.faculty_id)
    summary = {
        'roster': len(members),
        'linked': sum(1 for m in members if m.is_linked),
        'unlinked': sum(1 for m in members if not m.is_linked),
        'on_permission': sum(1 for r in rows if r['has_permission']),
    }

    return render_template(
        'hod/class_detail.html',
        user=current_user(),
        class_group=group,
        owner_name=owner.name if owner else '—',
        members=members,
        rows=rows,
        view_date=view_date,
        summary=summary,
    )


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


@hod_bp.route('/search')
@permission_required('hod.search')
def search():
    """Find a student by roll number or name and list their permissions."""
    user = current_user()
    query = (request.args.get('q') or '').strip()

    results = []
    if len(query) >= 2:
        results = _search_students(user, query)

    return render_template(
        'hod/search.html',
        user=user,
        query=query,
        results=results,
    )


def _search_students(viewer, query: str) -> list:
    """Return students matching the query, with their permission summary."""
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
        })
    return output


# --------------------------------------------------------------------------
# Faculty leave approvals
#
# The queue for leave that has been arranged and submitted. Kept apart from the
# student permission register below it: different entity, different evidence, and
# a different question -- "is this class going to be taught" rather than "may this
# student leave the campus".
# --------------------------------------------------------------------------

@hod_bp.route('/leave')
@permission_required('hod.leave_queue')
def leave_queue():
    """Faculty leave awaiting a decision, longest-waiting first.

    The HOD's to-do leads, with everything decided below it. An approval queue
    that mixes decided and undecided rows makes the undecided ones hard to find,
    which is the whole reason this list exists.
    """
    user = current_user()

    pending = LeaveModel.find_by_status(LeaveStatus.HOD_PENDING)
    decided = LeaveModel.find_by_status(LeaveStatus.DEAN_PENDING) \
        + LeaveModel.find_by_status(LeaveStatus.REJECTED)

    # An HOD's own application is not in this queue. They are teaching staff and
    # can file leave like anyone else, so their application arrives here like
    # everybody else's -- and it is not theirs to decide. It stays visible under
    # their own "My Leave" instead.
    pending = [l for l in pending if l.faculty_id != user.id]
    decided = [l for l in decided if l.faculty_id != user.id]

    for record in pending + decided:
        LeaveSummary.attach(record, AdjustmentModel.find_by_leave(record.id))
    _attach_leave_names(pending + decided)

    pending.sort(key=lambda l: (l.from_date or date.max, l.id))
    decided.sort(key=lambda l: (l.from_date or date.max, l.id), reverse=True)

    counts = {
        'awaiting': len(pending),
        'uncovered': sum(1 for l in pending if l.uncovered_count),
        'with_dean': sum(1 for l in decided
                         if l.status == LeaveStatus.DEAN_PENDING),
        'rejected': sum(1 for l in decided
                        if l.status == LeaveStatus.REJECTED),
    }

    return render_template(
        'hod/leave.html',
        user=user,
        pending=pending,
        decided=decided,
        counts=counts,
    )


@hod_bp.route('/leave/<int:leave_id>')
@permission_required('hod.leave_detail')
def leave_detail(leave_id: int):
    """One application in full, with the class adjustments behind it.

    The HOD decides whether the teaching is covered, so the adjustments are the
    page rather than an appendix: each period, who is covering it, and which ones
    nobody could staff. A period marked UNCOVERED is not an error to be tidied
    away -- it is the fact the approval is actually about.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None:
        abort(404)

    # An HOD opens their own application to read it, not to decide it. The
    # decision is refused in the service too; this keeps the page from
    # rendering approval buttons over the HOD's own request in the first place.
    own_application = leave.faculty_id == current_user().id

    adjustments = AdjustmentModel.find_by_leave(leave_id)
    LeaveSummary.attach(leave, adjustments)
    _attach_leave_names([leave])
    _attach_substitute_names(adjustments)

    applicant = UserModel.find_by_id(leave.faculty_id)
    slots = TimetableModel.slots_by_section(
        adjustments[0].section if adjustments else '')

    # Their other live applications, so the HOD can see this is not a second
    # request for the same days before approving it.
    others = [l for l in LeaveModel.find_open(leave.faculty_id,
                                              exclude_id=leave_id)
              if l.status != LeaveStatus.HOD_PENDING]

    return render_template(
        'hod/leave_detail.html',
        user=current_user(),
        leave=leave,
        applicant=applicant,
        adjustments=adjustments,
        slots=slots,
        others=others,
        own_application=own_application,
        coverage=TimetableModel.allocation_coverage(),
    )


@hod_bp.route('/leave/<int:leave_id>/action', methods=['POST'])
@permission_required('hod.leave_action')
def leave_action(leave_id: int):
    """Approve or refuse a faculty leave application.

    A remark is required either way, and required by name when an uncovered period
    is being approved over -- in that case the HOD is deciding that a class goes
    untaught, and a bare "approved" would leave nobody able to say who decided
    that or why.
    """
    user = current_user()
    approve = (request.form.get('decision') or '').lower() == 'approve'
    remarks = (request.form.get('remarks') or '').strip()

    try:
        leave = hod_act_on_leave(leave_id=leave_id, hod=user, approve=approve,
                                 remarks=remarks)
    except LeaveError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('hod.leave_detail', leave_id=leave_id))

    flash(
        f'Leave application #{leave_id} approved and passed to the Dean.'
        if approve else
        f'Leave application #{leave_id} rejected.',
        'success')
    return redirect(url_for('hod.leave_queue'))


@hod_bp.route('/leave/<int:leave_id>/letter')
@permission_required('hod.leave_letter')
def leave_letter(leave_id: int):
    """The approval letter, as the HOD prints it.

    Same document the applicant and the Dean receive. Only available once the
    Dean has signed, because before that there is nothing granted to certify.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None:
        abort(404)

    try:
        context = letter_context(leave)
    except LeaveError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('hod.leave_detail', leave_id=leave_id))

    return render_template('faculty/leave_letter.html', user=current_user(),
                           **context)


# ---------- leave helpers ----------

def _attach_leave_names(leaves) -> None:
    """Name the applicant on each leave, with one read for the whole list."""
    ids = {l.faculty_id for l in leaves if l.faculty_id}
    names = {}
    for person_id in ids:
        person = UserModel.find_by_id(person_id)
        if person:
            names[person_id] = person.name
    for leave in leaves:
        leave.faculty_name = names.get(leave.faculty_id)


def _attach_substitute_names(adjustments) -> None:
    """Name the colleague covering each period."""
    ids = {a.assigned_faculty_id for a in adjustments if a.assigned_faculty_id}
    names = {}
    for person_id in ids:
        person = UserModel.find_by_id(person_id)
        if person:
            names[person_id] = person.name
    for adjustment in adjustments:
        adjustment.assigned_name = names.get(adjustment.assigned_faculty_id)