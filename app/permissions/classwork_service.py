"""Creating classwork, and the objections it raises.

Two entry points, and the second one is the reason this module exists:

* `create_classwork` records what a lecturer has scheduled and, when it lands on
  somebody who already holds an approved permission, suspends that permission
  and raises an objection for the reviewer.
* `respond_to_objection` applies the reviewer's answer, which either restores the
  original grant or ends it.

Suspending rather than rejecting is deliberate. The permission is already a
printed letter in the student's pocket; overwriting its status to REJECTED would
leave the paper and the record contradicting each other, and the student would
find out at a gate. PENDING is the honest state for "granted, now disputed", and
`is_approved` on the request reads false the whole time, so nothing downstream --
the letter, the register, the daily report -- treats the student as having
permission while the objection is open.
"""

from datetime import date, time

from app.models import ApprovalAction, RequestStatus
from app.models.classwork import (
    CLASSWORK_IMPORTANCE_CHOICES, ClassworkImportance, ClassworkModel,
    ObjectionModel, ObjectionStatus, _overlaps,
)
from app.models.permission import (
    ApprovalModel, PermissionModel, attach_members,
)
from app.models.user import UserModel
from app.permissions.service import (
    PERIOD_TIMES, ValidationError, parse_date,
)
from app.utils import email as mailer

#: The default session spans, reused from the permission side so a classwork and
#: a request labelled "afternoon" mean the same hours in both places.
SESSION_TIMES = PERIOD_TIMES

#: Preset remarks offered when a reviewer declines to approve on the strength of
#: classwork. They fill the box rather than deciding anything -- the reviewer
#: still has to press the button that changes the status, because a one-click
#: rejection would put a student on record as refused for a reason they never
#: got to read or contest.
FAST_REPLY_PRESETS = (
    ('Attend the class — you have an assessment scheduled.',
     'The section has assessed work scheduled for this session.'),
    ('The classwork scheduled is more important than this event.',
     'The reviewing lecturer has marked the scheduled work as blocking.'),
    ('Event is not important enough to miss scheduled classwork.',
     'Submitted for an event of low consequence against fixed academic work.'),
    ('Reschedule the event, or attend and submit the work later.',
     'Where the work can be submitted after the fact, the request can stand.'),
    ('This repeats often — please plan events around the timetable.',
     'A pattern note, for the student to read on the request.'),
)


def importance_choices():
    return CLASSWORK_IMPORTANCE_CHOICES


def fast_reply_presets():
    return FAST_REPLY_PRESETS


def timetable_options(class_group, on_date: date):
    """The slots this section actually meets on a date, as form options.

    Read per section and per weekday rather than from the department-wide slot
    table, because the published timetable disagrees with itself: period 2 starts
    at 10:20 in most sections and 10:30 in the hub and interview ones. Offering
    "period 2" as 10:30 to a section that starts at 10:20 produces a classwork
    record that is wrong in the one field a reviewer will rely on.
    """
    from app.models.leave import TimetableModel
    from app.models.leave import DAY_NAMES

    section = (getattr(class_group, 'section_code', None) or '').strip()
    if not section or on_date is None:
        return []

    label = DAY_NAMES[on_date.weekday()].title()[:3]
    options = {}
    for row in TimetableModel.all_periods():
        if row.get('section') != section or row.get('day') != label:
            continue
        period = int(row.get('period') or 0)
        if not period:
            continue
        options[period] = (
            row.get('start'),
            row.get('end'),
            (row.get('subject_code') or row.get('course_code') or '').strip(),
        )

    return [
        {'period': period, 'start': start, 'end': end,
         'subject': subject, 'label': _slot_label(period, start, end, subject)}
        for period, (start, end, subject) in sorted(options.items())
    ]


def _slot_label(period, start, end, subject):
    clock = ''
    if start and end:
        clock = f' ({_hhmm(start)}–{_hhmm(end)})'
    name = f' · {subject}' if subject else ''
    return f'Period {period}{clock}{name}'


def _hhmm(value) -> str:
    """Format a stored slot time, which may be a string or a `time`."""
    if isinstance(value, time):
        return f'{value:%H:%M}'
    text = str(value or '')
    return text[:5] if len(text) >= 5 else text


def _parse_clock(value):
    from app.permissions.service import parse_time
    return parse_time(value, 'time')


def create_classwork(*, faculty, class_id, title: str, date_raw: str,
                     importance_raw: str, period_raw=None, session_raw=None,
                     notes: str = '', objection_reason: str = '',
                     base_url: str = ''):
    """Record one piece of classwork and suspend anything it clashes with.

    Returns `(classwork, objections)`. The objections list is what the lecturer
    needs to see immediately -- "this reopened 3 permissions" is the fact that
    matters, and finding out by email later is too late to be useful.
    """
    from app.models.classes import ClassModel

    group = ClassModel.find_by_id(class_id) if class_id else None
    if group is None:
        raise ValidationError('Choose a class from your own list.')
    # The class list is already filtered to this lecturer, but the id arrives
    # from a form, so ownership is checked rather than assumed.
    if group.faculty_id != faculty.id:
        raise ValidationError('That class is not one of yours.')
    # A proctor list is owned by the same lecturer and stores its students the
    # same way, so ownership alone would let it through. It is a mentoring
    # allocation with no section, no timetable and no subject -- there is nothing
    # to schedule assessed work against, and classwork recorded against it would
    # then surface on a student's permission review as though their mentor had
    # scheduled an exam.
    if (group.kind or 'CLASS') != 'CLASS':
        raise ValidationError(
            'Classwork is scheduled against a taught class, not a proctor list.'
        )

    title = (title or '').strip()
    if len(title) < 3:
        raise ValidationError('Name the classwork in at least 3 characters.')

    on = parse_date(date_raw, 'date')
    if on < date.today():
        raise ValidationError('Classwork cannot be dated in the past.')

    try:
        importance = ClassworkImportance((importance_raw or '').strip().upper())
    except ValueError:
        raise ValidationError('Choose how important the classwork is.')

    # Either a timetable slot or a named session, never both. A slot resolves to
    # real clock times; a named session resolves to the department's standard
    # span, which is the same mapping the permission form uses.
    period = None
    session = (session_raw or '').strip().lower() or None
    start_time = end_time = None

    period_raw = (str(period_raw).strip() if period_raw is not None else '')
    if period_raw:
        if not str(period_raw).isdigit():
            raise ValidationError('Choose a timetable slot.')
        period = int(period_raw)
        chosen = None
        for option in timetable_options(group, on):
            if option['period'] == period:
                chosen = option
                break
        if chosen is None:
            raise ValidationError(
                'That section does not meet in this slot on that date.'
            )
        session = None
        start_time = _parse_clock(chosen['start'])
        end_time = _parse_clock(chosen['end'])
    elif session:
        if session not in SESSION_TIMES:
            raise ValidationError(
                'Choose Full day, Morning or Afternoon.'
            )
        start_time, end_time = SESSION_TIMES[session]
    else:
        raise ValidationError(
            'Pick a timetable slot, or choose a session.'
        )

    entry = ClassworkModel.create(
        faculty_id=faculty.id,
        class_id=class_id,
        title=title,
        date_=on,
        importance=importance,
        start_time=start_time,
        end_time=end_time,
        period=period,
        period_label=session,
        notes=(notes or '').strip() or None,
    )

    objections = []
    if entry.raises_objections:
        objections = suspend_conflicting_requests(entry, faculty)
        for objection in objections:
            record = PermissionModel.find_by_id(objection.request_id)
            reviewer = (UserModel.find_by_id(record.assigned_faculty_id)
                        if record else None)
            student = (UserModel.find_by_id(record.student_id)
                       if record else None)
            if reviewer is not None and student is not None and base_url:
                mailer.notify_reviewer_of_objection(
                    record, student, reviewer, entry, faculty, base_url,
                )
            if student is not None and base_url:
                mailer.notify_student_of_objection(
                    record, student, entry, faculty, base_url,
                )

    return entry, objections


def suspend_conflicting_requests(entry, faculty):
    """Find granted permissions this classwork clashes with and suspend them.

    Only APPROVED permissions are touched. A request still in a reviewer's queue
    is already visible to that reviewer -- the panel will show the classwork
    without anybody having to do anything -- and suspending it would remove it
    from the one queue it is supposed to be sitting in.

    The whole request is suspended even when only one member of a group clashes,
    because status is a property of the request and not of its members. Which
    members are actually affected is recorded on the objection so the reviewer
    can see it, and the HOD can still strike members off when they decide.
    """
    from app.models.classes import MemberModel

    members = MemberModel.find_by_class(entry.class_id)
    if not members:
        return []

    by_id = {m.student_id for m in members if m.student_id}
    by_roll = {str(m.roll_number or '').strip().upper() for m in members
               if m.roll_number}

    raised = []
    for row in _approved_requests():
        record = PermissionModel._to_request(row)
        if not _overlaps(entry, record.start_date, record.end_date,
                         record.start_time, record.end_time):
            continue

        affected = _members_caught(record, by_id, by_roll)
        if not affected:
            continue

        if _open_objection_exists(record.id, entry.id):
            continue

        objection = ObjectionModel.create(
            request_id=record.id,
            classwork_id=entry.id,
            raised_by=faculty.id,
            reason=(entry.notes or entry.title),
        )
        # Copies the reviewer rather than re-deriving it, so the queue below
        # does not have to read the permission to know who should see it.
        ObjectionModel._store_reviewer(objection.id, record.assigned_faculty_id)

        PermissionModel.update_status(
            record.id, RequestStatus.PENDING,
            faculty_id=record.assigned_faculty_id,
        )
        ApprovalModel.create(
            record.id, faculty.id, ApprovalAction.SUSPENDED,
            f'Classwork scheduled after the permission was granted: '
            f'{entry.title} ({entry.date}).',
        )
        _record_affected_members(objection.id, affected)
        objection.affected_member_ids = affected
        raised.append(objection)

    return raised


def _approved_requests():
    from app.models.firestore import store

    return list(store.documents(
        'permission_requests', status=RequestStatus.APPROVED.value
    ))


def _members_caught(record, by_id, by_roll) -> list:
    """Which of a request's members sit in the classwork's section."""
    from app.models.firestore import store

    caught = []
    for member_id in record.members:
        if member_id in by_id:
            caught.append(member_id)
            continue
        row = store.get('users', member_id)
        roll = str((row or {}).get('roll_number') or '').strip().upper()
        if roll and roll in by_roll:
            caught.append(member_id)
    return caught


def _open_objection_exists(request_id: int, classwork_id: int) -> bool:
    from app.models.firestore import store

    for row in store.documents('classwork_objections', request_id=request_id):
        if row.get('classwork_id') == classwork_id:
            if row.get('status') == ObjectionStatus.PENDING.value:
                return True
    return False


def _record_affected_members(objection_id: int, member_ids) -> None:
    from app.models.firestore import store

    store.update('classwork_objections', objection_id,
                 {'affected_member_ids': list(member_ids)})


def classwork_for_request(record):
    """The panel the reviewer sees on a request: what clashes, and how badly.

    Sorted worst-first, because the reviewer's job is to weigh the heaviest thing
    on the day. A panel showing "slip test, project review" in calendar order
    invites the reader to treat them as equally weighted.
    """
    from app.models.classwork import attach_classwork_context

    entries = ClassworkModel.find_for_students(
        record.members, record.start_date, record.end_date
    )
    if not entries:
        return []

    order = list(ClassworkImportance)
    entries.sort(key=lambda c: (
        -order.index(c.importance), c.date or date.max,
        c.start_time or time.min,
    ))
    attach_classwork_context(entries)
    return entries


def respond_to_objection(*, objection_id: int, reviewer, accept: bool,
                         remarks: str = '', base_url: str = ''):
    """Apply the reviewer's answer to a raised objection.

    `accept=True` means the objection was well-founded: the classwork genuinely
    clashed with what was granted, and the original grant is put back rather
    than re-decided from scratch. The reviewer has already seen the evidence --
    they approved the request with the classwork in view, and were asked again
    once the lecturer recorded it -- so re-asking them to approve it a second
    time would be asking the same question twice.

    `accept=False` is the lecturer being overruled, and the permission ends.
    """
    objection = ObjectionModel.find_by_id(objection_id)
    if objection is None:
        raise ValidationError('That objection no longer exists.')
    if not objection.is_pending:
        raise ValidationError('This objection has already been answered.')

    record = PermissionModel.find_by_id(objection.request_id)
    if record is None:
        raise ValidationError('The permission request no longer exists.')

    remarks = (remarks or '').strip()
    from app.models.firestore import utc_now

    if accept:
        ObjectionModel.respond(objection.id, reviewer.id,
                               ObjectionStatus.ACCEPTED, remarks)
        PermissionModel.update_status(record.id, RequestStatus.APPROVED)
        ApprovalModel.create(record.id, reviewer.id, ApprovalAction.APPROVED,
                             remarks or 'Objection upheld; permission restored.')
        final_status = RequestStatus.APPROVED
    else:
        ObjectionModel.respond(objection.id, reviewer.id,
                               ObjectionStatus.REJECTED, remarks)
        PermissionModel.update_status(record.id, RequestStatus.REJECTED)
        ApprovalModel.create(record.id, reviewer.id, ApprovalAction.REJECTED,
                             remarks or 'Objection overruled; permission refused.')
        final_status = RequestStatus.REJECTED

    # Updated in place rather than re-read. The caller is handed this object and
    # the flash message is built from it, so returning the row as it stood before
    # the write would have the page report "Awaiting reviewer" immediately after
    # the reviewer answered it.
    objection.status = (
        ObjectionStatus.ACCEPTED if accept else ObjectionStatus.REJECTED
    )
    objection.responded_by = reviewer.id
    objection.responded_at = utc_now()
    objection.response_remarks = remarks

    _settle_sibling_objections(record.id, objection.id)

    if base_url:
        student = UserModel.find_by_id(record.student_id)
        if student is not None:
            mailer.notify_student_of_objection_decision(
                record, student, objection, reviewer,
                'restored' if accept else 'refused', remarks, base_url,
            )

    return objection, record, final_status


def _settle_sibling_objections(request_id: int, answered_id: int) -> None:
    """Close any other open objection on the same permission.

    The status is per request, so answering one objection has necessarily
    answered them all -- leaving the rest PENDING would show the request as
    "disputed" on a permission that is now decided. The decision the reviewer
    just made is recorded as their remarks rather than discarded.
    """
    from app.models.firestore import store

    for other in ObjectionModel.find_by_request(request_id):
        if other.id == answered_id or not other.is_pending:
            continue
        store.update('classwork_objections', other.id, {
            'status': ObjectionStatus.REJECTED.value,
            'response_remarks': 'Settled with the other objection on this '
                                'permission.',
        })