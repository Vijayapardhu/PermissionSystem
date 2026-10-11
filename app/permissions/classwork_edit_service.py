"""Edit and delete for classwork.

Two problems had to be settled before either could be written.

Editing a date or a session is not a cosmetic change. `create_classwork`
suspends every granted permission the work clashes with and opens an
objection on each. If a lecturer moves an exam from Tuesday to Thursday, the
objections raised against the Tuesday slot are now wrong -- they say a slip test
clashed with a permission on a date nothing now sits on. So an edit withdraws
the objections the old version raised and re-runs the clash check against the
new one. Anything that no longer clashes is left alone; anything that now
clashes is opened fresh.

Deleting is the same question with a harder answer, and the choice is
destructive-by-default on purpose. A lecturer who schedules a slip test, has it
suspend three permissions, and then deletes it has not changed their mind about
the exam -- they have cancelled it, and three students are still marked absent
from a class that no longer exists. Deleting therefore also puts those
permissions back to APPROVED and closes the objections. The alternative --
leaving a permission suspended on the strength of classwork that is gone -- is
a student's access to class decided by a deleted record, and nothing on the
page would say so.

Both are reversible in effect but not in fact: there is no undelete, so the
delete confirmation states exactly which permissions will be restored.
"""
from datetime import date, time

from app.models import ApprovalAction, RequestStatus
from app.models.classes import MemberModel
from app.models.classwork import (
    ClassworkImportance, ClassworkModel, ObjectionModel, ObjectionStatus,
)
from app.models.permission import ApprovalModel, PermissionModel
from app.permissions.service import ValidationError, parse_date

SESSION_LABELS = {
    'full_day': 'Full day',
    'morning': 'Morning',
    'afternoon': 'Afternoon',
}


def _owned_entry(entry, faculty) -> None:
    """Refuse anything that is not this lecturer's own classwork."""
    if entry is None:
        raise ValidationError('That classwork is not on your list.')
    if entry.faculty_id != faculty.id:
        # Deliberately not a 403-equivalent message: "not on your list" is both
        # true and does not confirm that the id exists to somebody else.
        raise ValidationError('That classwork is not on your list.')


def _resolve_slot(group, on, period_raw, session_raw):
    """Turn the two form inputs into (period, session, start, end).

    Shared with the create path so an edit and a create cannot disagree about
    what "period 3" means for a section on a date. Validation errors are the
    same strings either way, because the message is what the lecturer reads
    when the form is wrong and a different one on edit would look like a bug.
    """
    from app.permissions.classwork_service import (
        SESSION_TIMES, _parse_clock, timetable_options,
    )

    period = None
    session = (session_raw or '').strip().lower() or None
    start_time = end_time = None

    period_raw = (str(period_raw).strip() if period_raw is not None else '')
    if period_raw:
        if not period_raw.isdigit():
            raise ValidationError('Choose a timetable slot.')
        period = int(period_raw)
        chosen = next((o for o in timetable_options(group, on)
                       if o['period'] == period), None)
        if chosen is None:
            raise ValidationError(
                'That section does not meet in this slot on that date.'
            )
        session = None
        start_time = _parse_clock(chosen['start'])
        end_time = _parse_clock(chosen['end'])
    elif session:
        if session not in SESSION_TIMES:
            raise ValidationError('Choose Full day, Morning or Afternoon.')
        start_time, end_time = SESSION_TIMES[session]
    else:
        raise ValidationError('Pick a timetable slot, or choose a session.')

    return period, session, start_time, end_time


def update_classwork(*, entry, faculty, class_id, title: str, date_raw: str,
                     importance_raw: str, period_raw=None, session_raw=None,
                     notes: str = ''):
    """Change one piece of classwork, then re-check what it clashes with.

    Returns `(entry, opened, withdrawn)` so the lecturer is told what actually
    changed rather than being left to work it out from the new list. Both lists
    matter: an edit that quietly reopened four permissions is exactly the fact
    that must not be discovered later.
    """
    from app.models.classes import ClassModel

    _owned_entry(entry, faculty)

    group = ClassModel.find_by_id(class_id) if class_id else None
    if group is None:
        raise ValidationError('Choose a class from your own list.')
    if group.faculty_id != faculty.id:
        raise ValidationError('That class is not one of yours.')
    # Same guard as the create path. A proctor list is owned by the same
    # lecturer and stores students identically, so ownership alone lets it
    # through and classwork recorded against it would surface on a student's
    # permission review as though their mentor had scheduled an exam.
    if (group.kind or 'CLASS') != 'CLASS':
        raise ValidationError(
            'Classwork is scheduled against a taught class, not a proctor list.'
        )

    title = (title or '').strip()
    if len(title) < 3:
        raise ValidationError('Name the classwork in at least 3 characters.')

    on = parse_date(date_raw, 'date')
    # A date may be moved to today or later, never back. Re-dating into the
    # past would silently withdraw objections that a lecturer has already seen
    # answered, and would leave the clash check unable to raise anything for a
    # day that has already happened.

    try:
        importance = ClassworkImportance((importance_raw or '').strip().upper())
    except ValueError:
        raise ValidationError('Choose how important the classwork is.')

    period, session, start_time, end_time = _resolve_slot(
        group, on, period_raw, session_raw,
    )

    # Withdraw the objections the *previous* version raised, before writing the
    # new one. Done first so the clash check below cannot see its own stale
    # objections and skip work it should raise.
    withdrawn = _withdraw_objections(entry, faculty)

    ClassworkModel.update(
        entry.id,
        class_id=class_id,
        title=title,
        date=on,
        importance=importance,
        start_time=start_time,
        end_time=end_time,
        period=period,
        period_label=session,
        notes=(notes or '').strip() or None,
    )

    updated = ClassworkModel.find_by_id(entry.id)

    opened = []
    if updated.raises_objections:
        from app.permissions.classwork_service import suspend_conflicting_requests
        opened = suspend_conflicting_requests(updated, faculty)

    return updated, opened, withdrawn


def _withdraw_objections(entry, faculty):
    """Close this entry's own open objections, restoring what they suspended.

    Only this lecturer's, and only against this entry. An objection is a claim
    that *this piece of work* clashed with a grant; once the work changes, the
    claim is no longer true whatever the new version does, and the new version
    gets to make its own claims below.
    """
    from app.models.firestore import store

    settled = []
    for objection in ObjectionModel.find_pending():
        if objection.classwork_id != entry.id:
            continue
        if objection.raised_by != faculty.id:
            continue
        record = PermissionModel.find_by_id(objection.request_id)
        if record is not None:
            PermissionModel.update_status(
                record.id, RequestStatus.APPROVED,
                faculty_id=record.assigned_faculty_id,
            )
        store.update(
            'classwork_objections', objection.id,
            {'status': ObjectionStatus.WITHDRAWN.value},
            touch=False,
        )
        settled.append(record)

    return settled


def delete_classwork(*, entry, faculty):
    """Remove one entry, restoring every permission its objections suspended.

    Returns the permissions that were put back, so the confirmation can name
    them. Silent restoration would be the wrong call: a student whose access
    just came back should be findable in the reviewer's own queue, and the
    lecturer should know they caused it.
    """
    from app.models.firestore import store

    _owned_entry(entry, faculty)

    restored = _withdraw_objections(entry, faculty)

    for objection in ObjectionModel.find_pending():
        if objection.classwork_id == entry.id:
            store.update(
                'classwork_objections', objection.id,
                {'status': ObjectionStatus.WITHDRAWN.value},
                touch=False,
            )

    ClassworkModel.delete(entry.id)
    return restored