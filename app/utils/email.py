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
    # Three different facts, three different wordings. A lecturer's approval is
    # final now; a forward is a verification on its way to the HOD, so the
    # wording has to say the permission is not granted yet rather than claim it
    # is.
    if action == ApprovalAction.APPROVED:
        subject = f'Your permission request #{request.id} has been approved'
        headline = (
            'your permission request has been approved by '
            f'{faculty.name}. This is the final decision.'
        )
    elif action == ApprovalAction.FORWARDED:
        subject = f'Permission request #{request.id} verified and sent to the HOD'
        headline = (
            'your lecturer has verified it and forwarded it to the Head of '
            'Department for the final approval. It is not yet a grant of '
            'permission.'
        )
    else:
        subject = 'Your permission request was rejected'
        headline = 'your permission request has been rejected.'

    link = f'{base_url}/student/requests/{request.id}'
    body = (
        f'Hello {student.name},\n\n'
        f'{headline}\n\n'
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


def notify_member_added_to_request(request: PermissionRequest, requester: User,
                                    member: User, faculty: User,
                                    base_url: str) -> bool:
    """Tell a student they have been added to someone else's permission.

    Silence here would be the unfair part of the feature: a student could be
    covered by a letter they never saw submitted, discover it only when a gate
    asks for it, and have no way to ask why.
    """
    link = f'{base_url}/student/requests/{request.id}'
    subject = f'You are covered by permission request REQ-{request.id:04d}'
    body = (
        f'Hello {member.name},\n\n'
        f'{requester.name} has submitted a permission request that covers you '
        f'as well.\n\n'
        f'Type         : {request.permission_type.value.title()}\n'
        f'From         : {request.start_date}\n'
        f'To           : {request.end_date}\n'
        f'Reason       : {request.reason}\n'
        f'Submitted by : {requester.name}\n\n'
        f'This is not a grant of permission yet. It is reviewed by '
        f'{faculty.name} and then by the Head of Department, and you will be '
        f'emailed when a decision is made.\n\n'
        f'View the request:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [member.email], body)


def notify_member_not_covered(request: PermissionRequest, requester: User,
                              member: User, hod: User, base_url: str) -> bool:
    """Tell a student the HOD struck them off a group before approving it."""
    link = f'{base_url}/student/requests/{request.id}'
    subject = f'You are not covered by permission request REQ-{request.id:04d}'
    body = (
        f'Hello {member.name},\n\n'
        f'{requester.name} submitted a group permission that included you, and '
        f'the Head of Department has approved it without you.\n\n'
        f'From         : {request.start_date}\n'
        f'To           : {request.end_date}\n'
        f'Decided by   : {hod.name}\n\n'
        f'This letter does not cover you. If that looks wrong, speak to '
        f'{hod.name}.\n\n'
        f'View the request:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [member.email], body)


def notify_student_of_hod_decision(request: PermissionRequest, student: User,
                                   hod: User, action: ApprovalAction,
                                   remarks: str, base_url: str) -> bool:
    """The escalated verdict, which is the one the student's letter carries.

    Reached both by an HOD deciding a request a lecturer forwarded, and by
    an HOD or Deputy HOD taking one directly. `hod` is whichever of them
    signed it, and the wording follows them.
    """
    verb = 'approved' if action == ApprovalAction.APPROVED else 'rejected'
    # The office comes from the decision-maker's own role. A Deputy HOD
    # signs this too, and printing them as the Head of Department would put
    # a title on the letter that they do not hold.
    office = {
        'HOD': 'the Head of Department',
        'DEPUTY_HOD': 'the Deputy Head of Department',
    }.get(getattr(getattr(hod, 'role', None), 'value', ''), 'the department')
    subject = f'Your permission request was {verb} by {office}'
    link = f'{base_url}/student/requests/{request.id}'
    body = (
        f'Hello {student.name},\n\n'
        f'Your permission request has been {verb} by the Head of Department.\n\n'
        f'Request ID   : #{request.id}\n'
        f'Type         : {request.permission_type.value.title()}\n'
        f'From         : {request.start_date}\n'
        f'To           : {request.end_date}\n'
        f'Decided by   : {hod.name}\n'
        f'Remarks      : {remarks or "-"}\n\n'
        f'View the request and print your letter:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [student.email], body)


def notify_hod_of_recommendation(request: PermissionRequest, student: User,
                                 faculty: User, hod: User, remarks: str,
                                 base_url: str) -> bool:
    """Ask the HOD for the decision the workflow reserves to them.

    Sent when a lecturer verifies a request they cannot permit themselves and
    forwards it. Kept under its historical name so existing callers and tests
    keep reading.
    """
    link = f'{base_url}/hod/requests/{request.id}'
    subject = f'Approval needed - Request #{request.id} verified by faculty'
    body = (
        f'Hello {hod.name},\n\n'
        f'A permission request has been verified by the faculty and now '
        f'needs your final approval.\n\n'
        f'Student      : {student.name} ({student.roll_number or "N/A"})\n'
        f'Type         : {request.permission_type.value.title()}\n'
        f'From         : {request.start_date}\n'
        f'To           : {request.end_date}\n'
        f'Faculty      : {faculty.name}\n'
        f'Remarks      : {remarks or "-"}\n\n'
        f'Approve or reject:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [hod.email], body)


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


# --- Classwork objections -------------------------------------------------
#
# Three messages. The awkward one is the middle: telling a student only that a
# permission is suspended, without saying what suspended it, leaves them unable
# to appeal; telling them classwork clashes without saying the permission is not
# granted right now leaves them holding a letter that no longer stands. So the
# suspension mail carries both and leads with the fact that matters.


def notify_reviewer_of_objection(request: PermissionRequest, student: User,
                                 reviewer: User, classwork, raiser: User,
                                 base_url: str) -> bool:
    """Ask the original reviewer to re-decide a permission that was suspended."""
    link = f'{base_url}/faculty/requests/{request.id}'
    subject = f'Classwork clashes with Request #{request.id} — review again'
    body = (
        f'Hello {reviewer.name},\n\n'
        f'{raiser.name} has scheduled classwork that clashes with a permission '
        f'you granted. The permission has been suspended and is back with you.\n\n'
        f'Request ID   : #{request.id}\n'
        f'Student      : {student.name} ({student.roll_number or "N/A"})\n'
        f'For          : {request.reason}\n'
        f'Dates        : {request.start_date} to {request.end_date}\n\n'
        f'Classwork    : {classwork.title}\n'
        f'Section      : {classwork.class_name or "-"}\n'
        f'Date         : {classwork.date}\n'
        f'Session      : {classwork.session_label}\n'
        f'Importance   : {classwork.importance_label}\n'
        f'Note         : {classwork.notes or "-"}\n\n'
        f'Uphold the objection to put the permission back, or overrule it to '
        f'refuse the request.\n\n'
        f'Reopen the request:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [reviewer.email], body)


def notify_student_of_objection(request: PermissionRequest, student: User,
                                classwork, raiser: User,
                                base_url: str) -> bool:
    """Tell the student their permission is suspended, and why."""
    link = f'{base_url}/student/requests/{request.id}'
    subject = f'Request #{request.id} suspended — classwork clashes'
    body = (
        f'Hello {student.name},\n\n'
        f'Your permission request has been suspended by the department and is '
        f'being looked at again. Please do not treat it as granted until you '
        f'hear otherwise.\n\n'
        f'Request ID   : #{request.id}\n'
        f'For          : {request.reason}\n'
        f'Dates        : {request.start_date} to {request.end_date}\n\n'
        f'Reason       : classwork has been scheduled that clashes with these '
        f'dates.\n'
        f'Classwork    : {classwork.title}\n'
        f'Date         : {classwork.date}\n'
        f'Importance   : {classwork.importance_label}\n'
        f'Scheduled by : {raiser.name}\n\n'
        f'You will be notified of the outcome.\n\n'
        f'View the request:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [student.email], body)


def notify_student_of_objection_decision(request: PermissionRequest, student: User,
                                         objection, reviewer: User,
                                         outcome: str, remarks: str,
                                         base_url: str) -> bool:
    """The outcome of a disputed permission, in the words the student reads.

    `outcome` is a plain word rather than a status enum so the wording here is
    the only place that decides what restoring a permission is called to a
    student.
    """
    link = f'{base_url}/student/requests/{request.id}'
    subject = f'Request #{request.id} {outcome} after review'
    body = (
        f'Hello {student.name},\n\n'
        f'The classwork raised against your request has been reviewed. Your '
        f'permission has been {outcome}.\n\n'
        f'Request ID   : #{request.id}\n'
        f'For          : {request.reason}\n'
        f'Dates        : {request.start_date} to {request.end_date}\n'
        f'Objection    : {objection.status_label}\n'
        f'Decided by   : {reviewer.name}\n'
        f'Reason       : {remarks or "-"}\n\n'
        f'View the request:\n{link}\n\n'
        f'--\n{current_app.config["APP_NAME"]}'
    )
    return _send(subject, [student.email], body)