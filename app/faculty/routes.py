import io
import mimetypes
import re

from contextlib import contextmanager
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.models import (
    ApprovalAction, LEAVE_TYPE_CHOICES, LeaveStatus, LeaveType, PermissionType,
    RequestStatus, UserRole,
)
from app.models.classes import (
    ClassModel, MemberModel, ProctorModel,
    KIND_CLASS, KIND_PROCTOR,
)
from app.models.classwork import (
    ClassworkModel, ObjectionModel, attach_classwork_context,
)
from app.models.leave import (
    AdjustmentModel, LeaveModel, LeaveSummary, TimetableModel,
)
from app.models.permission import (
    ApprovalModel, PermissionModel, ProofModel, attach_members,
    classify_route,
)
from app.models.user import UserModel
from app.permissions.leave_service import (
    LeaveError, MAX_LEAVE_DAYS,
    apply_leave_edit as apply_leave_edit_service,
    preview_leave_edit,
    # The four below are aliased because the route that calls each one has the
    # same name. Without the alias the name inside the view body resolved to the
    # view itself, not the imported service function, and every one of those
    # buttons raised TypeError and returned a 500. Only reachable by posting the
    # form, which is why the unit tests missed it and an HTTP walk caught it.
    cancel_leave as cancel_leave_service,
    candidate_periods_for,
    confirm_affected,
    create_leave,
    letter_context,
    mark_uncovered as mark_uncovered_service,
    request_substitution as request_substitution_service,
    respond_to_substitution,
    submit_to_hod,
    suggest_faculty,
    withdraw_substitution as withdraw_substitution_service,
)
from app.permissions.classwork_service import (
    # Aliased for the same reason as the leave service above: the routes below
    # are called `create_classwork` and `respond_to_objection`, so importing
    # the service under its own name would rebind it to the view at import time.
    classwork_for_request,
    create_classwork as create_classwork_service,
    fast_reply_presets,
    importance_choices,
    respond_to_objection as respond_to_objection_service,
    timetable_options,
)
from app.permissions.pages import permission_required
from app.permissions.service import ValidationError, act_on_request, student_can_view
from app.utils.changes import diff_fields, summarise
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


def annotate_events(requests):
    """Stamp the HOD-managed event each request names, if any.

    One read of the small events collection for the whole list, never one per
    row. A failure leaves every name unset rather than breaking the register:
    the event is context on the row, not the row itself.
    """
    from app.models.events import EventModel

    wanted = {getattr(record, 'event_id', None) for record in requests}
    wanted.discard(None)
    names = {}
    if wanted:
        try:
            for event in EventModel.list_all():
                if event.id in wanted:
                    names[event.id] = event.name
        except Exception:
            names = {}
    for record in requests:
        record.event_name = names.get(getattr(record, 'event_id', None))
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

    # Why is each request here at all?
    #
    # Routing sends a request to the student's proctor, but only if the college
    # has actually loaded the proctor lists -- and when it has not, the request
    # falls through to the least-loaded lecturer in the department. From inside
    # the queue those two look identical: the same list, the same buttons. A
    # lecturer cannot tell which students are theirs to mentor and which landed
    # on them by arithmetic, and that difference decides whether they are the
    # right person to be answering.
    #
    # So each entry is tagged rather than filtered. Hiding the fallback requests
    # would be worse: an HOD who named a lecturer as the hackathon coordinator
    # would find that lecturer unable to see the hackathon requests.
    proctored = ProctorModel.student_ids_for_faculty(user.id)
    for record in pending:
        record.from_proctor_list = bool(proctored & set(record.members))
    proctor_queue = sum(1 for r in pending if r.from_proctor_list)

    # Their OWN decided requests, not the department's. This used to be
    # `find_all_for_hod(limit=50)`, which is every request in the department --
    # so every lecturer could read every other student's roll number, reason and
    # outcome on what is meant to be their own queue, while being unable to see
    # the requests they had personally signed.
    history = PermissionModel.find_touching_faculty(user.id, limit=50)
    annotate_routes(history)

    return render_template(
        'faculty/dashboard.html',
        user=user,
        pending=pending,
        recent=history,
        pending_count=len(pending),
        proctor_queue=proctor_queue,
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
    annotate_events([record])
    student = UserModel.find_by_id(record.student_id)
    proofs = ProofModel.find_by_request(request_id)
    history = ApprovalModel.find_by_request(request_id)

    # What the section has scheduled across the dates this request covers, worst
    # first. This is the whole reason the module exists: the reviewer decides
    # with the classwork in front of them rather than from memory. Guarded
    # because a failure here must not hide the request itself -- losing the
    # ability to approve is worse than approving without the panel.
    try:
        scheduled = classwork_for_request(record)
    except Exception:
        current_app.logger.exception(
            'Classwork panel failed for request %s', request_id)
        scheduled = []

    objections = ObjectionModel.find_by_request(request_id)

    return render_template(
        'faculty/request_detail.html',
        user=user,
        request=record,
        student=student,
        proofs=proofs,
        history=history,
        classwork=scheduled,
        objections=objections,
        fast_replies=fast_reply_presets(),
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
    """Classes of the viewing lecturer that contain this roll number.

    Taught classes only. The proctor relationship is not a class the student
    attends, and listing it here as one put a row on the search results reading
    like a subject the student is enrolled in.
    """
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
        and (row.get('kind') or KIND_CLASS) == KIND_CLASS
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


@faculty_bp.route('/classes/<int:class_id>/edit', methods=['GET', 'POST'])
@permission_required('faculty.edit_class')
def edit_class(class_id: int):
    """Rename a class group, or correct its section and year.

    Same two steps as every other edit: the form shows what each field holds now,
    and the POST previews the differences rather than applying them. The owner is
    not editable here -- see `ClassModel.update_details`.
    """
    user = current_user()
    group = _owned_class(user, class_id)

    if request.method == 'GET':
        return render_template('faculty/class_edit.html', user=user,
                               group=group)

    changes, error = _class_changes(group, request.form)
    if error:
        flash(error, 'danger')
        return render_template('faculty/class_edit.html', user=user,
                               group=group, form=request.form)

    if not changes:
        flash('Nothing was changed.', 'info')
        return redirect(url_for('faculty.class_detail', class_id=class_id))

    return render_template('faculty/class_edit_confirm.html', user=user,
                           group=group, changes=changes, form=request.form)


@faculty_bp.route('/classes/<int:class_id>/edit/apply', methods=['POST'])
@permission_required('faculty.edit_class')
def apply_class_edit(class_id: int):
    """Write a confirmed class edit."""
    user = current_user()
    _owned_class(user, class_id)

    group = ClassModel.find_by_id(class_id)
    changes, error = _class_changes(group, request.form)
    if error:
        flash(error, 'danger')
        return redirect(url_for('faculty.edit_class', class_id=class_id))
    if not changes:
        flash('Nothing was changed.', 'info')
        return redirect(url_for('faculty.class_detail', class_id=class_id))

    # Re-checked here rather than trusting the preview: between the two, another
    # tab could have renamed a different class to this one.
    clash = ClassModel.find_by_name(request.form.get('name', ''),
                                    exclude_id=class_id)
    if clash is not None:
        flash(f'Another class is already called "{clash.name}".', 'danger')
        return redirect(url_for('faculty.edit_class', class_id=class_id))

    ClassModel.update_details(
        class_id,
        request.form.get('name', ''),
        request.form.get('section_code', ''),
        request.form.get('academic_year', ''),
    )
    flash(f'{summarise(changes)} Saved.', 'success')
    return redirect(url_for('faculty.class_detail', class_id=class_id))


CLASS_EDIT_LABELS = {
    'name': 'Class name',
    'section_code': 'Section',
    'academic_year': 'Year',
}


def _class_changes(group, form):
    """(changes, error) for a proposed class edit."""
    name = (form.get('name') or '').strip()
    if len(name) < 3:
        return [], 'Give the class a name of at least 3 characters.'
    clash = ClassModel.find_by_name(name, exclude_id=group.id)
    if clash is not None:
        return [], f'Another class is already called "{clash.name}".'
    return diff_fields(
        {'name': group.name, 'section_code': group.section_code,
         'academic_year': group.academic_year},
        {'name': name,
         'section_code': (form.get('section_code') or '').strip(),
         'academic_year': (form.get('academic_year') or '').strip()},
        CLASS_EDIT_LABELS,
    ), None


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

    rows = []
    for member in members:
        records = permissions_by_student.get(member.student_id, [])
        rows.append({
            'member': member,
            'permissions': records,
            'has_permission': bool(records),
        })

    summary = {
        'roster': len(members),
        'linked': sum(1 for m in members if m.is_linked),
        'unlinked': sum(1 for m in members if not m.is_linked),
        'on_permission': sum(1 for r in rows if r['has_permission']),
    }

    # The page opens on the students who have permission and hides the rest.
    #
    # A lecturer opening a section asks "who is missing today", and scrolling a
    # 68-row roster to find the four who are away is work repeated daily.
    # The full roster stays one click away, because "who am I teaching" is the
    # other half of the question and hiding it entirely would be its own wrong
    # answer.
    #
    # A link rather than a form: this is a view preference, not state, and it
    # belongs in the URL so the browser's back button undoes it.
    show_all = request.args.get('view') == 'all'
    visible_rows = rows if show_all else [r for r in rows if r['has_permission']]

    return render_template(
        'faculty/class_detail.html',
        user=user,
        class_group=group,
        members=members,
        rows=visible_rows,
        show_all=show_all,
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

    Separate from the classes they teach. A class has a timetable and a
    subject; a proctor list only decides whose permission requests land in
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

    # Who among the students already on this list also proctors others, and who
    # the list's own pending requests are -- so the page answers "is this list
    # doing anything" rather than only "how many names are on it".
    proctored_ids = ProctorModel.student_ids_in_group(group_id)
    pending = [r for r in PermissionModel.find_pending_for_faculty(user.id)
               if proctored_ids & set(r.members)]

    return render_template(
        'faculty/proctor_detail.html',
        user=user,
        group=group,
        members=members,
        summary=summary,
        pending_count=len(pending),
    )


@faculty_bp.route('/proctor/<int:group_id>/search')
@permission_required('faculty.proctor_group_detail')
def proctor_student_search(group_id: int):
    """Find students to add to a proctor list, by roll number or name.

    A JSON endpoint rather than a full page reload, because the thing a proctor
    does most is correct a list one name at a time, and re-rendering the whole
    page per keystroke would make that unusable. Only students who are already
    on some roster in this department come back, so a proctor cannot add an
    arbitrary person from the directory by typing part of a name.
    """
    user = current_user()
    _owned_group(user, group_id, KIND_PROCTOR)

    query = (request.args.get('q') or '').strip()
    results = ProctorModel.search_students(query) if len(query) >= 2 else []
    already = {m.roll_number for m in MemberModel.find_by_class(group_id)
               if m.roll_number}
    for row in results:
        row['already'] = row['roll_number'] in already

    return jsonify({'results': results})


@faculty_bp.route('/proctor/<int:group_id>/add', methods=['POST'])
@permission_required('faculty.upload_proctor_roster')
def add_proctor_student(group_id: int):
    """Put one student on a proctor list.

    Spreadsheet upload already covers a whole batch, but the case that comes up
    daily is different: one student is allocated to this proctor after the list
    was uploaded, and making them edit the spreadsheet and re-upload everything
    to add a single name is how proctor lists end up quietly out of date.
    """
    user = current_user()
    _owned_group(user, group_id, KIND_PROCTOR)

    raw = (request.form.get('roll') or '').strip().upper()
    back = url_for('faculty.proctor_group_detail', group_id=group_id)

    if not raw:
        flash('Enter a roll number.', 'danger')
        return redirect(back)

    if not re.match(r'^[0-9]{2}[A-Z0-9]{6,12}$', raw):
        flash(f'"{raw}" is not a roll number in the college format.', 'danger')
        return redirect(back)

    student = UserModel.find_by_roll_number(raw)
    if student is None or not student.is_active:
        flash(f'No student is registered with roll number {raw}.', 'danger')
        return redirect(back)

    if student.role != UserRole.STUDENT:
        flash(f'{raw} is not a student.', 'danger')
        return redirect(back)

    result = ProctorModel.add_students(group_id, [raw])
    if result.get('added'):
        flash(f'{raw} added to the proctor list. Their permission requests '
              f'will now come to you.', 'success')
    else:
        flash(f'{raw} is already on this proctor list.', 'info')
    return redirect(back)


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
    """Every request this lecturer is answerable for, with filters.

    Scoped to them, deliberately. The browser used to read the whole department
    through `_filtered_requests` with no owner filter, so any lecturer could page
    through up to 300 requests belonging to other students -- roll numbers,
    reasons and outcomes included -- which is not a queue, it is a register the
    department publishes to everyone in it.

    `include_decided` keeps their own signed-off work visible after it has left
    their queue, which is the other half of what a lecturer needs from this page.
    """
    user = current_user()

    status_filter = _status_filter(request.args.get('status'))
    type_filter = _type_filter(request.args.get('type'))
    date_from = _parse_date(request.args.get('from'), None)
    date_to = _parse_date(request.args.get('to'), None)
    search = (request.args.get('q') or '').strip()

    records = _filtered_requests(status_filter, type_filter,
                                 date_from, date_to, search, limit=300,
                                 faculty_id=user.id, include_decided=True)

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


# --------------------------------------------------------------------------
# Faculty leave and class adjustment
#
# A separate block from the student permission flow above, and deliberately so:
# different entities, a different approval chain and a different set of people
# involved. What they share is the shell, the navigation and the store.
# --------------------------------------------------------------------------

@faculty_bp.route('/leave')
@permission_required('faculty.my_leave')
def my_leave():
    """The signed-in lecturer's own applications, newest first."""
    user = current_user()

    leaves = LeaveModel.find_for_faculty(user.id)
    for record in leaves:
        LeaveSummary.attach(record, AdjustmentModel.find_by_leave(record.id))

    upcoming = [l for l in leaves
                if l.status not in (LeaveStatus.REJECTED, LeaveStatus.CANCELLED)
                and l.to_date >= date.today()]
    counts = {
        'total': len(leaves),
        'pending': sum(1 for l in leaves if l.status in (
            LeaveStatus.DRAFT, LeaveStatus.ADJUSTMENT_PENDING,
            LeaveStatus.ADJUSTMENT_COMPLETE)),
        'awaiting_hod': sum(1 for l in leaves
                            if l.status == LeaveStatus.HOD_PENDING),
        'approved': sum(1 for l in leaves
                        if l.status in (LeaveStatus.DEAN_PENDING,
                                        LeaveStatus.FINAL_APPROVED)),
        'rejected': sum(1 for l in leaves if l.status == LeaveStatus.REJECTED),
    }

    return render_template(
        'faculty/leave.html',
        user=user,
        leaves=leaves,
        upcoming=upcoming,
        counts=counts,
    )


@faculty_bp.route('/leave/new', methods=['GET', 'POST'])
@permission_required('faculty.apply_leave')
def apply_leave():
    """Step 1: the leave itself, before any period is chosen.

    Kept separate from the period picker so a partially filled application
    survives a stray click -- the dates and reason are already stored as a DRAFT
    by the time anyone reaches step 2.
    """
    user = current_user()

    if request.method == 'GET':
        return render_template(
            'faculty/leave_new.html',
            user=user,
            leave_types=LEAVE_TYPE_CHOICES,
            max_days=MAX_LEAVE_DAYS,
        )

    try:
        leave = create_leave(
            faculty=user,
            leave_type=request.form.get('leave_type', ''),
            from_date_raw=request.form.get('from_date', ''),
            to_date_raw=request.form.get('to_date', ''),
            reason=request.form.get('reason', ''),
        )
    except LeaveError as exc:
        flash(str(exc), 'danger')
        return render_template(
            'faculty/leave_new.html',
            user=user,
            leave_types=LEAVE_TYPE_CHOICES,
            max_days=MAX_LEAVE_DAYS,
            form=request.form,
        )

    return redirect(url_for('faculty.my_leave_detail', leave_id=leave.id))


@faculty_bp.route('/leave/<int:leave_id>')
@permission_required('faculty.my_leave_detail')
def my_leave_detail(leave_id: int):
    """The affected periods, who is covering each, and the shortlist for the rest."""
    user = current_user()
    leave = _own_leave(user, leave_id)

    adjustments = AdjustmentModel.find_by_leave(leave_id)
    LeaveSummary.attach(leave, adjustments)
    slots = TimetableModel.slots_by_section(leave_section(adjustments))

    coverage = TimetableModel.allocation_coverage()
    open_now = leave.status in (LeaveStatus.DRAFT,
                               LeaveStatus.ADJUSTMENT_PENDING)

    # What the applicant may tick, with the ones already recorded pre-ticked.
    # Rebuilt on every visit rather than stored, so the list reflects the current
    # timetable: a course dropped since they last looked should not still be
    # waiting there, ticked.
    already = {(a.date, p) for a in adjustments for p in a.period_list}
    candidate_periods = candidate_periods_for(user.id, leave) if open_now else []
    for entry in candidate_periods:
        entry['checked'] = (entry['date'], entry['period']) in already

    # One shortlist per unarranged period. Built here rather than on demand so the
    # page is a single render, and skipped entirely once the leave is with the HOD
    # -- after that the set of people who could help has changed anyway.
    shortlists = {}
    if open_now:
        for adjustment in adjustments:
            if adjustment.needs_action and not adjustment.assigned_faculty_id:
                shortlists[adjustment.id] = suggest_faculty(adjustment, user)

    attach_faculty_names(adjustments)

    return render_template(
        'faculty/leave_detail.html',
        user=user,
        leave=leave,
        adjustments=adjustments,
        candidate_periods=candidate_periods,
        slots=slots,
        coverage=coverage,
        shortlists=shortlists,
        open_now=open_now,
        # Empty for a draft, which has nothing to amend: there is nothing before
        # it to differ from.
        amendments=LeaveModel.amendments(leave_id),
    )


@faculty_bp.route('/leave/<int:leave_id>/letter')
@permission_required('faculty.leave_letter')
def leave_letter(leave_id: int):
    """The applicant's own approval letter.

    Available only once the Dean has signed, which is the moment the leave is
    actually granted and there is something to certify. Before that the detail
    page shows the application as still in a queue, and this returns to it.
    """
    user = current_user()
    leave = _own_leave(user, leave_id)

    try:
        context = letter_context(leave)
    except LeaveError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('faculty.my_leave_detail', leave_id=leave_id))

    return render_template('faculty/leave_letter.html', user=user, **context)


@faculty_bp.route('/leave/<int:leave_id>/periods', methods=['POST'])
@permission_required('faculty.confirm_periods')
def confirm_periods(leave_id: int):
    """Step 2: the applicant confirms which periods they are actually missing."""
    user = current_user()
    leave = _own_leave(user, leave_id)

    if leave.status not in (LeaveStatus.DRAFT, LeaveStatus.ADJUSTMENT_PENDING):
        flash('This application is past the stage where periods can be changed.',
              'warning')
        return redirect(url_for('faculty.my_leave_detail', leave_id=leave_id))

    try:
        confirm_affected(leave, user, request.form.getlist('period'))
    except LeaveError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('faculty.my_leave_detail', leave_id=leave_id))

    flash('Periods recorded. Arrange a substitute for each one below.', 'success')
    return redirect(url_for('faculty.my_leave_detail', leave_id=leave_id))


@faculty_bp.route('/leave/periods/<adjustment_id>/request', methods=['POST'])
@permission_required('faculty.request_substitution')
def request_substitution(adjustment_id: str):
    """Offer one period to one colleague."""
    user = current_user()
    faculty_id = request.form.get('faculty_id', type=int)
    if not faculty_id:
        flash('Choose a lecturer first.', 'danger')
        return redirect(url_for('faculty.my_adjustments'))

    try:
        request_substitution_service(adjustment_id, user, faculty_id)
    except LeaveError as exc:
        flash(str(exc), 'danger')
    else:
        flash('Request sent. They will see it in their adjustment inbox.', 'success')
    return redirect(url_for('faculty.my_adjustments'))


@faculty_bp.route('/leave/periods/<adjustment_id>/withdraw', methods=['POST'])
@permission_required('faculty.withdraw_substitution')
def withdraw_substitution(adjustment_id: str):
    user = current_user()
    try:
        withdraw_substitution_service(adjustment_id, user)
    except LeaveError as exc:
        flash(str(exc), 'danger')
    else:
        flash('Request withdrawn.', 'success')
    return redirect(url_for('faculty.my_leave'))


@faculty_bp.route('/leave/periods/<adjustment_id>/uncovered', methods=['POST'])
@permission_required('faculty.mark_uncovered')
def mark_uncovered(adjustment_id: str):
    """Flag a period the applicant cannot staff, for the HOD to decide on."""
    user = current_user()
    try:
        mark_uncovered_service(adjustment_id, applicant=user)
    except LeaveError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('faculty.my_leave'))

    flash('Marked as uncovered. Your HOD will see it and can approve over it.',
          'warning')
    return redirect(url_for('faculty.my_leave'))


@faculty_bp.route('/leave/<int:leave_id>/submit', methods=['POST'])
@permission_required('faculty.submit_leave')
def submit_leave(leave_id: int):
    user = current_user()
    try:
        submit_to_hod(leave_id, user)
    except LeaveError as exc:
        flash(str(exc), 'danger')
    else:
        flash('Sent to the HOD for approval.', 'success')
    return redirect(url_for('faculty.my_leave_detail', leave_id=leave_id))


@faculty_bp.route('/leave/<int:leave_id>/edit', methods=['GET', 'POST'])
@permission_required('faculty.edit_leave')
def edit_leave(leave_id: int):
    """Amend the four fields the applicant owns.

    Two steps on purpose. The form shows what each field holds right now beside
    the input, so you can see what you are about to replace; the POST does not
    write, it renders a confirmation listing every field that differs. Only the
    confirmation writes.

    Editing is allowed once the application is with the HOD. It stays with the
    HOD, and the amendment is written to the record's history so the decision is
    taken on something the HOD can see the change to.
    """
    user = current_user()
    leave = _own_leave(user, leave_id)

    if request.method == 'GET':
        return render_template(
            'faculty/leave_edit.html',
            user=user,
            leave=leave,
            leave_types=list(LeaveType),
            today=date.today(),
        )

    try:
        proposed = preview_leave_edit(
            leave_id, user,
            request.form.get('leave_type', ''),
            request.form.get('from_date', ''),
            request.form.get('to_date', ''),
            request.form.get('reason', ''),
        )
    except LeaveError as exc:
        flash(str(exc), 'danger')
        return render_template(
            'faculty/leave_edit.html',
            user=user,
            leave=leave,
            leave_types=list(LeaveType),
            today=date.today(),
            form=request.form,
        )

    if not proposed['changes']:
        flash('Nothing was changed.', 'info')
        return redirect(url_for('faculty.my_leave_detail', leave_id=leave_id))

    return render_template(
        'faculty/leave_edit_confirm.html',
        user=user,
        leave=leave,
        changes=proposed['changes'],
        form=request.form,
    )


@faculty_bp.route('/leave/<int:leave_id>/edit/apply', methods=['POST'])
@permission_required('faculty.edit_leave')
def apply_leave_edit(leave_id: int):
    """Write a confirmed amendment and record what it changed."""
    user = current_user()
    _own_leave(user, leave_id)

    try:
        result = apply_leave_edit_service(
            leave_id, user,
            request.form.get('leave_type', ''),
            request.form.get('from_date', ''),
            request.form.get('to_date', ''),
            request.form.get('reason', ''),
        )
    except LeaveError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('faculty.edit_leave', leave_id=leave_id))

    changes = result['changes']
    if not changes:
        flash('Nothing was changed.', 'info')
    elif result.get('recorded'):
        flash(
            f'Changes saved. {len(changes)} field'
            f'{"s" if len(changes) != 1 else ""} amended and recorded; your HOD '
            f'can see what changed.', 'success')
    else:
        flash(f'{summarise(changes)} Saved.', 'success')

    return redirect(url_for('faculty.my_leave_detail', leave_id=leave_id))


@faculty_bp.route('/leave/<int:leave_id>/cancel', methods=['POST'])
@permission_required('faculty.cancel_leave')
def cancel_leave(leave_id: int):
    user = current_user()
    try:
        cancel_leave_service(leave_id, user)
    except LeaveError as exc:
        flash(str(exc), 'danger')
    else:
        flash('Application withdrawn.', 'success')
    return redirect(url_for('faculty.my_leave'))


@faculty_bp.route('/adjustments')
@permission_required('faculty.my_adjustments')
def my_adjustments():
    """Substitution requests sent to the signed-in lecturer.

    The mirror of the applicant's page: they chase, this answers. One list, one
    accept or decline each, and the reason has to be given when declining so the
    applicant can pick somebody else instead of waiting.
    """
    user = current_user()

    pending = AdjustmentModel.find_pending_for(user.id)
    attach_requester_names(pending)
    accepted = AdjustmentModel.find_accepted_for(user.id)
    attach_requester_names(accepted)

    return render_template(
        'faculty/adjustments.html',
        user=user,
        pending=pending,
        accepted=accepted,
    )


@faculty_bp.route('/adjustments/<adjustment_id>/respond', methods=['POST'])
@permission_required('faculty.respond_adjustment')
def respond_adjustment(adjustment_id: str):
    user = current_user()
    accept = request.form.get('decision') == 'accept'
    remarks = request.form.get('remarks') or ''

    try:
        respond_to_substitution(adjustment_id, user, accept, remarks)
    except LeaveError as exc:
        flash(str(exc), 'danger')
    else:
        flash('Thank you -- the period is yours.' if accept
              else 'Declined. They have been told.', 'success')
    return redirect(url_for('faculty.my_adjustments'))


# ---------- leave helpers ----------

def _own_leave(user, leave_id: int):
    """A leave the viewer may act on: their own, or any for the HOD.

    A lecturer is refused somebody else's application rather than shown a
    read-only version, because there is nothing on such a page they could do with
    it, and a 403 is clearer than an empty screen.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None:
        abort(404)
    if leave.faculty_id != user.id and not user.is_hod:
        abort(403)
    return leave


def leave_section(adjustments) -> str:
    """The section these adjustments belong to, for the clock-time lookup."""
    for adjustment in adjustments:
        if adjustment.section:
            return adjustment.section
    return ''


def attach_faculty_names(adjustments) -> None:
    """Name the substitutes, with one read for the whole page."""
    ids = {a.assigned_faculty_id for a in adjustments if a.assigned_faculty_id}
    if not ids:
        return
    names = {}
    for person_id in ids:
        person = UserModel.find_by_id(person_id)
        if person:
            names[person_id] = person.name
    for adjustment in adjustments:
        adjustment.assigned_name = names.get(adjustment.assigned_faculty_id)


def attach_requester_names(adjustments) -> None:
    """Name the applicant on each request in the inbox."""
    ids = {a.original_faculty_id for a in adjustments if a.original_faculty_id}
    names = {}
    for person_id in ids:
        person = UserModel.find_by_id(person_id)
        if person:
            names[person_id] = person.name
    for adjustment in adjustments:
        adjustment.requester_name = names.get(adjustment.original_faculty_id)


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
                       search, limit=200, faculty_id=None,
                       include_decided=False):
    """Shared filter query used by the faculty browser and HOD request list.

    `faculty_id` narrows to the requests assigned to that lecturer.
    `include_decided` additionally brings in the ones they have already
    signed which have since moved on: a lecturer's browser is their own
    record, not the department's, and an approval they gave last week is
    still their work even after it left their queue.
    """
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

    rows = list(store.documents('permission_requests', **filters))

    if faculty_id and include_decided:
        seen = {row['id'] for row in rows}
        for entry in store.documents('approval_history', faculty_id=faculty_id):
            request_id = entry.get('request_id')
            if not request_id or request_id in seen:
                continue
            original = store.get('permission_requests', request_id)
            if original is not None:
                seen.add(request_id)
                rows.append(original)

    requests = [PermissionModel._to_request(row) for row in rows]

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


# --- Classwork -------------------------------------------------------------
#
# A lecturer records what their section has scheduled, and the reviewer's queue
# shows it. Recording work that clashes with a permission that was already
# granted suspends that permission and raises an objection, which is the part of
# this that has to be right: the grant is not overwritten, it is withdrawn back
# into the queue with the reason attached.


@faculty_bp.route('/classwork')
@permission_required('faculty.classwork')
def classwork():
    """The lecturer's own classwork, and the form to add one.

    One page rather than a list and a separate form. The lecturer's reason for
    being here is almost always "I am about to put something on a date", and
    making that two navigations deep is how the feature goes unused.
    """
    user = current_user()
    classes = ClassModel.find_for_faculty(user.id, kind=KIND_CLASS)

    entries = ClassworkModel.find_for_faculty(user.id)
    attach_classwork_context(entries)

    # Slot options are per class *and* per date, so they are fetched for the
    # class and date currently chosen on the form rather than for all of them.
    # Every combination up front is a full timetable scan per class per day.
    slots = []
    chosen_class = request.args.get('class_id', type=int)
    chosen_date = _parse_date(request.args.get('date'), date.today())
    if chosen_class:
        group = next((c for c in classes if c.id == chosen_class), None)
        if group is not None:
            slots = timetable_options(group, chosen_date)

    return render_template(
        'faculty/classwork.html',
        user=user,
        classes=classes,
        entries=entries,
        slots=slots,
        importance_choices=importance_choices(),
        chosen_class=chosen_class,
        chosen_date=chosen_date,
    )


@faculty_bp.route('/classwork/create', methods=['POST'])
@permission_required('faculty.create_classwork')
def create_classwork():
    """Record the classwork, and suspend whatever it clashes with."""
    user = current_user()
    try:
        entry, objections = create_classwork_service(
            faculty=user,
            class_id=request.form.get('class_id', type=int),
            title=request.form.get('title', ''),
            date_raw=request.form.get('date', ''),
            importance_raw=request.form.get('importance', ''),
            period_raw=request.form.get('period'),
            session_raw=request.form.get('session'),
            notes=request.form.get('notes', ''),
            base_url=APP_ROOT,
        )
    except ValidationError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('faculty.classwork'))

    # The count is the fact the lecturer needs right now. "Saved" alone would
    # let them close the tab believing nothing was affected, and only discover
    # later that three students had lost a permission they were told was granted.
    if objections:
        flash(
            f'Classwork recorded. {len(objections)} granted '
            f'{"permission was" if len(objections) == 1 else "permissions were"} '
            f'suspended and sent back to the reviewer.',
            'warning',
        )
    else:
        flash('Classwork recorded.', 'success')

    return redirect(url_for('faculty.classwork'))


@faculty_bp.route('/objections')
@permission_required('faculty.objections')
def objections():
    """Open objections on permissions this person granted."""
    user = current_user()
    pending = ObjectionModel.find_pending_for_faculty(user.id)
    return render_template(
        'faculty/objections.html',
        user=user,
        objections=pending,
    )


@faculty_bp.route('/objections/<int:objection_id>/respond', methods=['POST'])
@permission_required('faculty.respond_objection')
def respond_objection(objection_id: int):
    """Uphold or overrule an objection.

    `uphold` restores the permission: the objection was well-founded, so the
    original grant stands and the letter the student holds is valid again.
    `overrule` ends it -- the lecturer who raised it has been overruled by the
    person who granted the permission in the first place.
    """
    user = current_user()
    objection = ObjectionModel.find_by_id(objection_id)
    if objection is None:
        abort(404)

    # Only the reviewer the objection was addressed to may answer it. Checked
    # here rather than in the service because the service is also reachable from
    # a handover path that has its own idea of who the reviewer is.
    if objection.assigned_faculty_id and objection.assigned_faculty_id != user.id:
        abort(403)

    decision = (request.form.get('decision') or '').strip().lower()
    if decision not in ('uphold', 'overrule'):
        flash('Choose whether to uphold or overrule the objection.', 'danger')
        return redirect(url_for('faculty.request_detail',
                                request_id=objection.request_id))

    remarks = (request.form.get('remarks') or '').strip()

    try:
        _, record, final = respond_to_objection_service(
            objection_id=objection_id,
            reviewer=user,
            accept=(decision == 'uphold'),
            remarks=remarks,
            base_url=APP_ROOT,
        )
    except ValidationError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('faculty.request_detail',
                                request_id=objection.request_id))

    if final == RequestStatus.APPROVED:
        flash(f'Objection upheld. Request #{record.id} is approved again.', 'success')
    else:
        flash(f'Objection overruled. Request #{record.id} is refused.', 'success')

    return redirect(url_for('faculty.request_detail', request_id=record.id))
