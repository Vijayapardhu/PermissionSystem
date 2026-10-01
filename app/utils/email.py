import logging

from flask import current_app
from flask_mail import Mail, Message

from app.models import ApprovalAction, PermissionRequest, User
from app.models.permission import ApprovalModel

mail = Mail()

log = logging.getLogger(__name__)


def _send(subject: str, recipients, body: str, html: str = None) -> bool:
    """Send a message without ever breaking the request flow.

    SMTP on a university tenant is frequently rate-limited or misconfigured;
    a notification failure must not roll back a submitted request.
    """
    if not recipients:
        return False
    if not current_app.config.get('MAIL_USERNAME'):
        log.info('SMTP not configured; email suppressed: %s', subject)
        return False

    message = Message(
        subject=subject,
        recipients=[r for r in recipients if r],
        body=body,
        html=html,
        sender=current_app.config['MAIL_DEFAULT_SENDER'],
    )
    try:
        mail.send(message)
        return True
    except Exception:
        log.exception('Failed to send notification email: %s', subject)
        return False


def notify_faculty_of_new_request(request: PermissionRequest, student: User,
                                  faculty: User, base_url: str) -> bool:
    link = f'{base_url}/faculty/requests/{request.id}'
    subject = f'New Permission Request - {student.roll_number or student.name}'
    body = (
        f'Hello {faculty.name},\n\n'
        f'A new permission request has been submitted and needs your review.\n\n'
        f'Student      : {student.name}\n'
        f'Roll No      : {student.roll_number or "N/A"}\n'
        f'Type         : {request.permission_type.value.title()}\n'
        f'From         : {request.start_date}\n'
        f'To           : {request.end_date}\n'
        f'Reason       : {request.reason}\n\n'
        f'Review the request here:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [faculty.email], body)


def notify_faculty_of_requester_only(request: PermissionRequest, student: User,
                                    faculty: User, base_url: str) -> bool:
    return notify_faculty_of_new_request(request, student, faculty, base_url)


def notify_student_of_decision(request: PermissionRequest, student: User,
                               faculty: User, action: ApprovalAction,
                               remarks: str, base_url: str) -> bool:
    verb = 'approved' if action == ApprovalAction.APPROVED else 'rejected'
    subject = f'Your permission request was {verb}'
    link = f'{base_url}/student/requests/{request.id}'
    body = (
        f'Hello {student.name},\n\n'
        f'Your permission request has been {verb}.\n\n'
        f'Request ID   : #{request.id}\n'
        f'Type         : {request.permission_type.value.title()}\n'
        f'From         : {request.start_date}\n'
        f'To           : {request.end_date}\n'
        f'Reviewed by  : {faculty.name}\n'
        f'Remarks      : {remarks or "-"}\n\n'
        f'View the request:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [student.email], body)


def notify_hod_of_decision(request: PermissionRequest, student: User,
                           faculty: User, action: ApprovalAction,
                           hod: User, remarks: str, base_url: str) -> bool:
    verb = 'approved' if action == ApprovalAction.APPROVED else 'rejected'
    subject = f'Request #{request.id} {verb} by faculty'
    link = f'{base_url}/hod/requests/{request.id}'
    body = (
        f'Hello {hod.name},\n\n'
        f'A permission request has just been {verb}.\n\n'
        f'Student      : {student.name} ({student.roll_number or "N/A"})\n'
        f'Type         : {request.permission_type.value.title()}\n'
        f'Faculty      : {faculty.name}\n'
        f'Remarks      : {remarks or "-"}\n\n'
        f'View on the dashboard:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [hod.email], body)