from datetime import date, datetime, time

from flask import current_app

from app.models import (
    ApprovalAction, OFFERABLE_PERMISSION_TYPES, PermissionType, RequestStatus,
    User,
)
from app.models.permission import (
    ApprovalModel, PermissionModel, ProofModel, normalise_members,
)
from app.models.user import UserModel
from app.utils import email as mailer
from app.utils.files import UploadError, validate_and_store
ACTIVITY_REASONS = ('CLUB', 'EVENT', 'WORKSHOP', 'SPORTS', 'INTERVIEW', 'OTHER')

# An activity permission covers an event, not an absence, so a fortnight is
# generous. Named for what it limits rather than for the type it retired with.
MAX_PERMISSION_DAYS = 14


class ValidationError(Exception):
    """Raised when submitted request data is unusable."""


class DuplicateRequestError(ValidationError):
    """Raised when a new request overlaps one a member of the group already has.

    Subclasses ValidationError so every existing `except ValidationError` still
    catches it. Carries the clashes rather than only their count, and each clash
    names the student it belongs to -- on a group permission "you already have a
    request on those dates" is not actionable when the clash is actually a
    classmate's.

    The student is not blocked outright: a genuine second permission on
    overlapping days is legitimate, but they have to acknowledge what is on file.
    """

    def __init__(self, clashes):
        self.clashes = list(clashes)
        # Kept as plain requests for anything that only needs the records.
        self.conflicts = [c['request'] for c in self.clashes]
        count = len(self.clashes)
        if count == 1:
            who = self.clashes[0].get('student')
            who = getattr(who, 'roll_number', None) or getattr(who, 'name', None)
            detail = f' ({who})' if who else ''
        else:
            detail = ''
        super().__init__(
            f'{count} of the students on this request already '
            f'{"has" if count == 1 else "have"} a live permission covering '
            f'those dates{detail}. Review the details below, then confirm to '
            f'submit anyway.'
        )


def resolve_members(requester: User, member_ids=None) -> list:
    """Validate the group and return its students, requester first.

    Every added member is checked rather than trusted: the ids arrive from a form
    and a request that names a staff account, a stranger from another department,
    or an account that has since been deactivated would put all three on one
    departmental letter. The count is checked before normalising, so a
    hand-rolled POST asking for ten students is told so rather than silently
    truncated to four.
    """
    from app.models.permission import MAX_GROUP_MEMBERS

    requested = []
    for candidate in list(member_ids or []):
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            raise ValidationError('One of the selected students is not valid.')
        if value not in requested:
            requested.append(value)

    distinct = len({*requested, requester.id})
    if distinct > MAX_GROUP_MEMBERS:
        raise ValidationError(
            f'A permission can cover at most {MAX_GROUP_MEMBERS} students on '
            f'one letter. Remove {distinct - MAX_GROUP_MEMBERS} and submit again.'
        )

    ordered = normalise_members(requester.id, requested)
    resolved = []
    for member_id in ordered:
        user = UserModel.find_by_id(member_id)
        if user is None or not user.is_active:
            raise ValidationError(
                'One of the students on this request is no longer active. '
                'Remove them and submit again.'
            )
        if not user.is_student:
            raise ValidationError(
                'Only students can be added to a permission request.'
            )
        if user.id != requester.id and user.department != requester.department:
            raise ValidationError(
                f'{user.roll_number or user.name} is not in your department, '
                f'so they cannot be covered by your permission.'
            )
        resolved.append(user)
    return resolved


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
    from app.models.firestore import store

    # The load is counted per lecturer rather than ordered in the database:
    # Firestore cannot sort by a field it did not filter on without a composite
    # index, and a request that 500s until an index exists in the console is a
    # worse failure than counting a handful of documents here.
    open_counts = {}
    for row in store.documents('permission_requests',
                               status=RequestStatus.PENDING.value):
        faculty_id = row.get('assigned_faculty_id')
        if faculty_id:
            open_counts[faculty_id] = open_counts.get(faculty_id, 0) + 1

    lecturers = UserModel.get_lecturers()
    if not lecturers:
        raise ValidationError(
            'No lecturer is currently available to review requests. '
            'Contact the HOD office.'
        )

    chosen = min(lecturers,
                 key=lambda u: (open_counts.get(u.id, 0), u.name or ''))
    return chosen


def submit_request(*, student: User, permission_type: str, reason: str,
                   start_date_raw: str, end_date_raw: str, start_time_raw: str,
                   end_time_raw: str, proof_file, base_url: str,
                   duplicate_ack: bool = False,
                   member_ids=None) -> int:
    """Validate, persist the request and its proof, then notify the reviewer."""
    # Checked against the offerable list, not the enum: leave management is
    # retired but `PermissionType.LEAVE` still exists so historical rows can be
    # read. Validating against the enum would keep accepting a type the form no
    # longer offers, which is how a retired permission quietly comes back.
    try:
        chosen = PermissionType((permission_type or '').strip().upper())
    except ValueError:
        raise ValidationError('Choose a permission type.')
    if chosen not in OFFERABLE_PERMISSION_TYPES:
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
    if (end_date - start_date).days > MAX_PERMISSION_DAYS:
        raise ValidationError(
            f'An activity permission cannot span more than {MAX_PERMISSION_DAYS} days.'
        )

    # Resolved before the duplicate check because the check has to run over every
    # member, and an id that is not a real student in this department must not be
    # queried for either.
    members = resolve_members(student, member_ids)
    member_id_list = [m.id for m in members]

    # The duplicate check runs before the proof is uploaded, not after. Storing
    # the file first and then refusing would leave an object in the bucket that
    # belongs to no request, and the student would be asked to re-pick it anyway
    # on the confirming submit.
    clashes = PermissionModel.find_overlapping_for_students(
        member_id_list, start_date, end_date
    )
    if clashes and not duplicate_ack:
        # Name the student on each clash, so the message is about a person rather
        # than about an abstract overlap.
        for clash in clashes:
            if clash.get('student') is None:
                clash['student'] = UserModel.find_by_id(clash['student_id'])
        raise DuplicateRequestError(clashes)

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
        permission_type=chosen,
        reason=reason,
        start_date=start_date,
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
        assigned_faculty_id=faculty.id,
        member_ids=member_id_list,
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
    for member in members:
        if member.id != student.id:
            mailer.notify_member_added_to_request(
                request_record, student, member, faculty, base_url
            )
    return request_record.id


def act_on_request(*, request_id: int, faculty: User, action: ApprovalAction,
                   remarks: str, base_url: str) -> None:
    """Apply a lecturer's decision, record history, and notify student + HOD.

    An approval here is a *recommendation*, not the grant: the request moves to
    AWAITING_HOD and only the HOD can turn it into an APPROVED permission. A
    rejection is final, because there is nothing left for the HOD to decide once
    the reviewing lecturer has refused it.

    The student's notification says which of the two happened. Telling them
    "approved" here and then having the HOD reject it is the one way this design
    could still lose a student's trust.
    """
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        raise ValidationError('That request no longer exists.')
    if record.status != RequestStatus.PENDING:
        raise ValidationError('That request has already been actioned.')

    student = UserModel.find_by_id(record.student_id)
    if student is None:
        raise ValidationError('The student record is missing.')

    status = (
        RequestStatus.AWAITING_HOD
        if action == ApprovalAction.APPROVED
        else RequestStatus.REJECTED
    )

    PermissionModel.update_status(request_id, status, faculty_id=faculty.id)
    ApprovalModel.create(request_id, faculty.id, action, (remarks or '').strip())

    mailer.notify_student_of_decision(
        record, student, faculty, action, remarks, base_url
    )

    if status == RequestStatus.AWAITING_HOD:
        for hod in UserModel.get_hods():
            mailer.notify_hod_of_recommendation(
                record, student, faculty, hod, remarks, base_url
            )
    else:
        for hod in UserModel.get_hods():
            mailer.notify_hod_of_decision(
                record, student, faculty, action, hod, remarks, base_url
            )


def hod_act_on_request(*, request_id: int, hod: User, action: ApprovalAction,
                       remarks: str, base_url: str,
                       member_ids=None) -> None:
    """The HOD's decision, which is the one that settles the permission.

    Only a request the lecturer has already recommended can be actioned here. A
    request still PENDING has not been reviewed by anybody, so treating it as
    approvable would hand the HOD a shortcut around the stage the workflow puts
    between them.

    On a group permission the HOD may strike members off before approving, which
    is the whole reason the approved subset is stored separately from the
    membership: "two of these four are covered" is a decision the letter and the
    register both have to be able to show afterwards. An approval that would
    leave nobody covered is refused rather than quietly recorded as covering
    everyone, which is the failure mode a defaulted subset would produce.
    """
    record = PermissionModel.find_by_id(request_id)
    if record is None:
        raise ValidationError('That request no longer exists.')
    if record.status != RequestStatus.AWAITING_HOD:
        raise ValidationError(
            'That request is not waiting on the HOD. A request has to be '
            'recommended by a lecturer first.'
        )

    student = UserModel.find_by_id(record.student_id)
    if student is None:
        raise ValidationError('The student record is missing.')

    approved = list(record.members)
    dropped = []
    if member_ids is not None and record.is_group:
        wanted = {int(m) for m in member_ids if str(m).isdigit()}
        kept = [m for m in record.members if m in wanted]
        if not kept:
            raise ValidationError(
                'An approval has to cover at least one student. Keep one member, '
                'or reject the whole request instead.'
            )
        if kept != approved:
            approved = kept
            # Derived from the decision just made, not from the record: the
            # approved subset has not been written yet, so `dropped_members`
            # would still be reading the old value and finding nobody.
            dropped = [m for m in record.members if m not in set(kept)]

    status = (
        RequestStatus.APPROVED
        if action == ApprovalAction.APPROVED
        else RequestStatus.REJECTED
    )

    PermissionModel.update_status(request_id, status)
    if status == RequestStatus.APPROVED and record.is_group:
        PermissionModel.set_approved_members(request_id, approved)
    ApprovalModel.create(request_id, hod.id, action, (remarks or '').strip())

    mailer.notify_student_of_hod_decision(
        record, student, hod, action, remarks, base_url
    )
    for member_id in dropped:
        member = UserModel.find_by_id(member_id)
        if member is not None:
            mailer.notify_member_not_covered(
                record, student, member, hod, base_url
            )


def student_can_view(record, user: User) -> bool:
    """A student may read a request they are on, not only one they filed.

    Someone added to a group permission has to be able to open it, read what it
    says about them and print their own copy of the letter. Staff see everything,
    as before.
    """
    if user.role.value == 'STUDENT':
        return user.id in record.members
    return True


def cancel_request(request_id: int, student: User) -> None:
    record = PermissionModel.find_by_id(request_id)
    # Withdrawing is the requester's alone on a group permission. Letting any
    # member cancel it would let one of four people void the letter for the other
    # three; a member who wants off should ask, or the HOD can strike them at
    # approval.
    if record is None or record.student_id != student.id:
        raise ValidationError('That request no longer exists.')
    # Withdrawable right up until the HOD decides. Once a permission is APPROVED
    # or REJECTED it is a departmental record and the student cannot retract it;
    # while it is still PENDING or AWAITING_HOD nobody has signed it, so letting
    # them withdraw is the honest answer rather than a dead end.
    if record.status not in (RequestStatus.PENDING, RequestStatus.AWAITING_HOD):
        raise ValidationError('Only a request still under review can be withdrawn.')
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