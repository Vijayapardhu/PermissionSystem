from contextlib import contextmanager
from datetime import date, datetime, time

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)

from app.models import RequestStatus, UserRole
from app.models.classes import MemberModel
from app.models.database import db
from app.models.permission import ApprovalModel, PermissionModel, ProofModel
from app.models.user import UserModel
from app.permissions.service import ValidationError, cancel_request, submit_request
from app.utils.qr import letter_qr
from app.utils.security import current_user, login_required, roles_required

student_bp = Blueprint('student', __name__, url_prefix='/student')


@contextmanager
def db_cursor(dictionary=True):
    with db.get_cursor(dictionary=dictionary) as cursor:
        yield cursor


@student_bp.route('/dashboard')
@roles_required(UserRole.STUDENT)
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
@roles_required(UserRole.STUDENT)
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
            )
        except ValidationError as exc:
            flash(str(exc), 'danger')
            return render_template(
                'student/new_request.html', user=user, form=request.form
            )

        flash(f'Request #{request_id} submitted and sent for review.', 'success')
        return redirect(url_for('student.requests'))

    return render_template(
        'student/new_request.html',
        user=user,
        form={},
        default_type=request.args.get('type', 'LEAVE').upper(),
    )


@student_bp.route('/requests')
@roles_required(UserRole.STUDENT)
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
@roles_required(UserRole.STUDENT, UserRole.LECTURER, UserRole.HOD)
def request_detail(request_id: int):
    user = current_user()
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        abort(404)

    if user.is_student and record.student_id != user.id:
        abort(403)

    student = UserModel.find_by_id(record.student_id)
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
@roles_required(UserRole.STUDENT)
def classes_for_student():
    """Classes this student belongs to, and their permissions in each."""
    user = current_user()

    MemberModel.relink_unresolved()

    with db_cursor() as cursor:
        cursor.execute(
            """SELECT c.id, c.name, c.section_code, c.academic_year, u.name AS faculty_name
               FROM class_members m
               JOIN class_groups c ON c.id = m.class_id
               LEFT JOIN users u ON u.id = c.faculty_id
               WHERE m.student_id = %s AND m.enrolled = TRUE
               ORDER BY c.name""",
            (user.id,),
        )
        groups = cursor.fetchall()

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
@roles_required(UserRole.STUDENT, UserRole.LECTURER, UserRole.HOD)
def request_letter(request_id: int):
    """Formal, printable permission letter for a single request."""
    user = current_user()
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        abort(404)

    if user.is_student and record.student_id != user.id:
        abort(403)

    student = UserModel.find_by_id(record.student_id)
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
@roles_required(UserRole.STUDENT)
def withdraw(request_id: int):
    try:
        cancel_request(request_id, current_user())
        flash('Request withdrawn.', 'success')
    except ValidationError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('student.requests'))