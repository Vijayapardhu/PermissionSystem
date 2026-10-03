from datetime import date, datetime, time

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for,
)

from app.models import RequestStatus
from app.models.classes import MemberModel
from app.models.permission import (
    ApprovalModel, PermissionModel, ProofModel, attach_members,
)
from app.models.user import UserModel
from app.permissions.pages import permission_required
from app.permissions.service import (
    DuplicateRequestError, ValidationError, cancel_request, resolve_members,
    student_can_view, submit_request,
)
from app.utils.qr import letter_qr
from app.utils.security import current_user, login_required

student_bp = Blueprint('student', __name__, url_prefix='/student')


@student_bp.route('/requests/new/members')
@permission_required('student.member_search')
def member_search():
    """Search the department directory for students to add to a request.

    Read-only and returns JSON, because it backs a typeahead rather than a page
    of its own. Scoped to the requester's own department and to active students
    only: a permission letter is a departmental record, so the picker must not
    offer a staff account or a student from another department -- and the service
    re-checks both anyway, because anything arriving from a form is untrusted.
    """
    user = current_user()
    query = (request.args.get('q') or '').strip()

    if len(query) < 2:
        return jsonify({'members': []})

    needle = query.lower()
    matches = []
    for candidate in UserModel.get_students():
        if candidate.id == user.id or candidate.department != user.department:
            continue
        haystack = f'{candidate.roll_number or ""} {candidate.name or ""}'.lower()
        if needle in haystack:
            matches.append({
                'id': candidate.id,
                'name': candidate.name,
                'roll_number': candidate.roll_number,
            })

    # A roll number sorts ahead of a name match, because somebody who already
    # knows the PIN wants that one student, not the twenty whose surname matches.
    matches.sort(key=lambda m: (0 if (m['roll_number'] or '').lower().startswith(needle)
                                else 1, m['roll_number'] or '', m['name'] or ''))
    return jsonify({'members': matches[:12]})


@student_bp.route('/dashboard')
@permission_required('student.dashboard')
def dashboard():
    user = current_user()
    requests = PermissionModel.find_by_student(user.id, limit=10)
    counts = {status: 0 for status in RequestStatus}
    for record in PermissionModel.find_by_student(user.id, limit=500):
        counts[record.status] = counts.get(record.status, 0) + 1

    return render_template(
        'student/dashboard.html',
        user=user,
        requests=requests,
        counts=counts,
        pending_count=counts.get(RequestStatus.PENDING, 0),
    )


@student_bp.route('/requests/new', methods=['GET', 'POST'])
@permission_required('student.new_request')
def new_request():
    user = current_user()

    if request.method == 'POST':
        try:
            request_id = submit_request(
                student=user,
                permission_type=request.form.get('permission_type', ''),
                reason=request.form.get('reason', ''),
                start_date_raw=request.form.get('start_date', ''),
                end_date_raw=request.form.get('end_date', ''),
                start_time_raw=request.form.get('start_time', ''),
                end_time_raw=request.form.get('end_time', ''),
                proof_file=request.files.get('proof'),
                base_url=url_for('faculty.dashboard', _external=True).rsplit('/', 1)[0],
                duplicate_ack=bool(request.form.get('duplicate_ack')),
                member_ids=request.form.getlist('member_ids'),
            )
        except DuplicateRequestError as exc:
            # Caught ahead of ValidationError: it is a subclass, so the order is
            # what keeps the overlapping requests on screen instead of only their
            # count, each labelled with the student it belongs to. The form comes
            # back with the student's values intact and the proof field cleared,
            # because nothing was uploaded on this attempt.
            flash(str(exc), 'warning')
            return render_template(
                'student/new_request.html',
                user=user,
                form=request.form,
                conflicts=exc.conflicts,
                clash_students={c['student_id']: c.get('student')
                                for c in exc.clashes},
                selected_members=resolve_members(
                    user, request.form.getlist('member_ids')),
            )
        except ValidationError as exc:
            flash(str(exc), 'danger')
            return render_template(
                'student/new_request.html', user=user, form=request.form,
                selected_members=resolve_members(
                    user, request.form.getlist('member_ids')),
            )

        flash(f'Request #{request_id} submitted and sent for review.', 'success')
        return redirect(url_for('student.requests'))

    return render_template(
        'student/new_request.html',
        user=user,
        form={},
        default_type='CLASSROOM',
    )


@student_bp.route('/requests')
@permission_required('student.requests')
def requests():
    user = current_user()
    status_filter = request.args.get('status')

    selected = None
    if status_filter:
        try:
            selected = RequestStatus(status_filter.upper())
        except ValueError:
            selected = None

    records = PermissionModel.find_by_student(user.id, status=selected, limit=200)
    return render_template(
        'student/requests.html',
        user=user,
        requests=records,
        selected_status=selected,
    )


@student_bp.route('/requests/<int:request_id>')
@permission_required('student.request_detail')
def request_detail(request_id: int):
    user = current_user()
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        abort(404)

    if user.is_student and not student_can_view(record, user):
        abort(403)

    student = UserModel.find_by_id(record.student_id)
    attach_members([record])
    proofs = ProofModel.find_by_request(request_id)
    history = ApprovalModel.find_by_request(request_id)

    return render_template(
        'student/request_detail.html',
        user=user,
        request=record,
        student=student,
        proofs=proofs,
        history=history,
    )


@student_bp.route('/classes')
@permission_required('student.classes_for_student')
def classes_for_student():
    """Classes this student belongs to, and their permissions in each."""
    from app.models.firestore import store

    user = current_user()

    MemberModel.relink_unresolved()

    # Roster rows, then the classes they point at, then their lecturers. Three
    # reads instead of one join, and each is a single-field lookup.
    class_ids = {
        row.get('class_id')
        for row in store.documents('class_members', student_id=user.id)
        if row.get('enrolled', True)
    }
    groups = [row for row in store.documents('class_groups')
              if row.get('id') in class_ids]
    faculty_names = {
        row['id']: row.get('name') for row in store.documents('users')
    }
    for group in groups:
        group['faculty_name'] = faculty_names.get(group.get('faculty_id'))
    groups.sort(key=lambda g: g.get('name') or '')

    today = date.today()
    active = PermissionModel.get_today_approved()
    mine = [r for r in active if r.student_id == user.id]

    return render_template(
        'student/classes.html',
        user=user,
        classes=groups,
        active_today=mine,
        today=today,
    )


@student_bp.route('/requests/<int:request_id>/letter')
@permission_required('student.request_letter')
def request_letter(request_id: int):
    """Formal, printable permission letter for a single request."""
    user = current_user()
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        abort(404)

    if user.is_student and not student_can_view(record, user):
        abort(403)

    student = UserModel.find_by_id(record.student_id)
    attach_members([record])
    proofs = ProofModel.find_by_request(request_id)
    history = ApprovalModel.find_by_request(request_id)

    faculty_name = '—'
    decision = None
    if history:
        latest = history[-1]
        faculty_name = getattr(latest, 'faculty_name', None) or '—'
        decision = latest

    # The QR points at the public verification page. A QR encoder that is
    # missing or failing yields an empty data URI and the letter prints without
    # a code, which is a far better outcome than a letter that will not print.
    try:
        qr = letter_qr(record.id)
    except Exception:
        current_app.logger.exception('Could not build the verification QR code')
        qr = {'url': '', 'image': '', 'reference': f'REQ-{record.id:04d}'}

    return render_template(
        'student/letter.html',
        user=user,
        request=record,
        student=student,
        proofs=proofs,
        history=history,
        faculty_name=faculty_name,
        decision=decision,
        qr=qr,
    )


@student_bp.route('/requests/<int:request_id>/withdraw', methods=['POST'])
@permission_required('student.withdraw')
def withdraw(request_id: int):
    try:
        cancel_request(request_id, current_user())
        flash('Request withdrawn.', 'success')
    except ValidationError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('student.requests'))