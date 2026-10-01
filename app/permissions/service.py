from datetime import date, datetime, time

from flask import current_app

from app.models import ApprovalAction, PermissionType, RequestStatus, User
from app.models.permission import ApprovalModel, PermissionModel, ProofModel
from app.models.user import UserModel
from app.utils import email as mailer
from app.utils.files import UploadError, validate_and_store

LEAVE_REASONS = ('MEDICAL', 'PERSONAL', 'FAMILY', 'TRAVELLING')
CLASSROOM_REASONS = ('CLUB', 'EVENT', 'WORKSHOP', 'SPORTS', 'INTERVIEW', 'OTHER')

MAX_LEAVE_DAYS = 30


class ValidationError(Exception):
    """Raised when submitted request data is unusable."""


def parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        raise ValidationError(f'Provide a valid {field}.')


def parse_time(value: str, field: str):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), '%H:%M').time()
    except ValueError:
        raise ValidationError(f'Provide a valid {field}.')


def pick_faculty(student: User) -> User:
    """Assign to the lecturer with the fewest open requests to spread load."""
    from app.models.database import db

    with db.get_cursor() as cursor:
        cursor.execute(
            """SELECT u.id,
                      (SELECT COUNT(*) FROM permission_requests pr
                        WHERE pr.assigned_faculty_id = u.id AND pr.status = 'PENDING'
                      ) AS open_count
               FROM users u
               WHERE u.role = 'LECTURER' AND u.is_active = TRUE
               ORDER BY open_count ASC, u.name ASC
               LIMIT 1"""
        )
        row = cursor.fetchone()
    if not row:
        raise ValidationError(
            'No lecturer is currently available to review requests. '
            'Contact the HOD office.'
        )
    return UserModel.find_by_id(row['id'])


def submit_request(*, student: User, permission_type: str, reason: str,
                   start_date_raw: str, end_date_raw: str, start_time_raw: str,
                   end_time_raw: str, proof_file, base_url: str) -> int:
    """Validate, persist the request and its proof, then notify the reviewer."""
    if permission_type not in PermissionType.__members__:
        raise ValidationError('Choose a permission type.')

    reason = (reason or '').strip()
    if len(reason) < 10:
        raise ValidationError('Describe the reason in at least 10 characters.')

    start_date = parse_date(start_date_raw, 'start date')
    end_date = parse_date(end_date_raw, 'end date')

    if start_date < date.today():
        raise ValidationError('The start date cannot be in the past.')
    if end_date < start_date:
        raise ValidationError('The end date cannot precede the start date.')
    if (end_date - start_date).days > MAX_LEAVE_DAYS:
        raise ValidationError(f'Requests cannot exceed {MAX_LEAVE_DAYS} days.')

    start_time = parse_time(start_time_raw, 'start time')
    end_time = parse_time(end_time_raw, 'end time')
    if start_time and end_time and end_time <= start_time:
        raise ValidationError('The end time must be after the start time.')

    try:
        stored = validate_and_store(proof_file)
    except UploadError as exc:
        raise ValidationError(str(exc))

    faculty = pick_faculty(student)

    request_record = PermissionModel.create(
        student_id=student.id,
        permission_type=PermissionType(permission_type),
        reason=reason,
        start_date=start_date,
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
        assigned_faculty_id=faculty.id,
    )

    ProofModel.create(
        request_id=request_record.id,
        original_filename=stored['original_filename'],
        stored_filename=stored['stored_filename'],
        file_path=stored['file_path'],
        file_type=stored['file_type'],
        file_size=stored['file_size'],
    )

    mailer.notify_faculty_of_new_request(
        request_record, student, faculty, base_url
    )
    return request_record.id


def act_on_request(*, request_id: int, faculty: User, action: ApprovalAction,
                   remarks: str, base_url: str) -> None:
    """Apply a faculty decision, record history, and notify student + HOD."""
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        raise ValidationError('That request no longer exists.')
    if record.status != RequestStatus.PENDING:
        raise ValidationError('That request has already been actioned.')

    student = UserModel.find_by_id(record.student_id)
    if student is None:
        raise ValidationError('The student record is missing.')

    status = (
        RequestStatus.APPROVED
        if action == ApprovalAction.APPROVED
        else RequestStatus.REJECTED
    )

    PermissionModel.update_status(request_id, status, faculty_id=faculty.id)
    ApprovalModel.create(request_id, faculty.id, action, (remarks or '').strip())

    mailer.notify_student_of_decision(
        record, student, faculty, action, remarks, base_url
    )

    for hod in UserModel.get_hods():
        mailer.notify_hod_of_decision(
            record, student, faculty, action, hod, remarks, base_url
        )


def student_can_view(record, user: User) -> bool:
    if user.role.value == 'STUDENT':
        return record.student_id == user.id
    return True


def cancel_request(request_id: int, student: User) -> None:
    record = PermissionModel.find_by_id(request_id)
    if record is None or record.student_id != student.id:
        raise ValidationError('That request no longer exists.')
    if record.status != RequestStatus.PENDING:
        raise ValidationError('Only pending requests can be withdrawn.')
    PermissionModel.update_status(request_id, RequestStatus.CANCELLED)


def categorize_reason(reason: str) -> str:
    """Bucket free-text reasons for the HOD analytics chart."""
    text = (reason or '').lower()
    buckets = (
        ('Medical', ('medical', 'health', 'doctor', 'hospital', 'ill', 'sick', 'fever')),
        ('Personal', ('personal', 'family', 'home', 'marriage')),
        ('Event', ('event', 'workshop', 'seminar', 'conference', 'club', 'sports')),
        ('Interview', ('interview', 'placement', 'internship')),
    )
    for label, keywords in buckets:
        if any(keyword in text for keyword in keywords):
            return label
    return 'Other'