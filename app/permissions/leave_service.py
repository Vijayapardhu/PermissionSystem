"""The leave workflow: affected classes, and who is free to take them.

This is the "smart" part of the plan, and it is rule-based on purpose. The
college's own brief says a matching algorithm beats a model here, and that is not
only a shortcut -- a rule that says "same course, same department, fewer accepted
substitutions" can be explained to an approver, checked by the faculty it affects,
and defended when someone is unhappy about being left out. A learned ranking
would be none of those things and would need retraining every semester.

The flow:

1. ``candidate_periods_for``  -- what the applicant could be missing. Derived from
   the courses they are listed to teach, because the published timetable names a
   teaching team per course but not who takes which slot.
2. ``confirm_affected``       -- the applicant picks the periods they really are
   missing. Their answer is authoritative and is what allocates their own rows.
3. ``suggest_faculty``        -- for each affected period, who could take it,
   ranked.
4. ``request_substitution`` / ``respond_to_substitution`` -- the request and the
   answer.

**On confidence.** `suggest_faculty` can only tell a lecturer is busy if their
periods have an owner. The college's export leaves every period unowned, so with
no allocation the honest answer is "nobody is known to be busy", which is not the
same as "everybody is free". So the ranking returns a `confidence` alongside the
names, ``allocation_coverage()`` supplies the underlying number, and the interface
says so rather than presenting a guess as a shortlist. Ranking still helps -- a
subject-eligible, same-department, lightly-loaded colleague is a better first
approach whatever the coverage -- but nobody is told the list is exhaustive when
it is not.
"""

import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

from app.models import (
    AdjustmentStatus, ClassAdjustment, FacultyLeave, LeaveStatus, LeaveType,
    UserRole,
)
from app.models.firestore import store
from app.models.leave import (
    ADJUSTMENTS, AMENDMENTS, COURSES, GROUPS, LEAVES, PERIODS,
    AdjustmentModel, LeaveModel, LeaveSummary, TimetableModel, _day_label,
)
from app.models.user import UserModel
from app.utils.changes import diff_fields

#: A single leave cannot span more than this. Long enough for a medical
#: absence, short enough that the affected-period list stays workable.
MAX_LEAVE_DAYS = 30

#: How each editable field is named in the change preview. Keys match the
#: document fields; the label is what a person would call it, which is rarely
#: the column name.
LEAVE_EDIT_LABELS = {
    'leave_type': 'Leave type',
    'from_date': 'From',
    'to_date': 'To',
    'reason': 'Reason',
}

#: How many suggestions to show. Enough to be a real choice, few enough that a
#: screen stays readable; the count of the rest is reported alongside.
SUGGESTION_LIMIT = 5

# Below this share of allocated periods, a shortlist is reported as provisional.
# Tuned to the current import, which allocates nothing, and meant to rise as the
# department enters its allocation rather than be a permanent shrug.
LOW_CONFIDENCE_COVERAGE = 0.4

# Scoring weights, in points. They sum to 95 rather than 100: availability is a
# precondition rather than a preference, so a busy lecturer is not "lower" than
# an idle one, they are ineligible. The remaining 5 is headroom for a future
# signal (room availability, a subject specialism score) that can be added without
# re-basing every weight.
SCORE_AVAILABLE = 40
SCORE_SAME_DEPARTMENT = 20
SCORE_SAME_COURSE = 20
WORKLOAD_BONUS_MAX = 15
WORKLOAD_BONUS_DIVISOR = 1


class LeaveError(Exception):
    """Raised when leave data cannot be acted on.

    Deliberately not the student permission service's ``ValidationError``: a leave
    problem caught as a permission problem would be reported with the wrong
    wording and could mask a real fault in one of the two flows.
    """


#: States an applicant may send for approval from.
#:
#: DRAFT is included because a leave whose dates touch no class has nothing to
#: confirm and would otherwise never reach an approver. The two adjustment states
#: are included because they are the normal outcome of confirming periods. HOD
#: onwards is not: once the approver has it, the applicant cannot take it back.
SUBMITTABLE_STATUSES = (
    LeaveStatus.DRAFT,
    LeaveStatus.ADJUSTMENT_PENDING,
    LeaveStatus.ADJUSTMENT_COMPLETE,
)


# --------------------------------------------------------------------------
# step 1 -- the application itself
# --------------------------------------------------------------------------

def parse_date(value, field: str) -> date:
    from datetime import datetime
    try:
        return datetime.strptime((value or '').strip(), '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        raise LeaveError(f'Provide a valid {field}.')


def create_leave(*, faculty, leave_type: str, from_date_raw: str,
                 to_date_raw: str, reason: str) -> FacultyLeave:
    """Validate and store a leave in DRAFT, before any periods are chosen."""
    try:
        chosen = LeaveType((leave_type or '').strip().upper())
    except ValueError:
        raise LeaveError('Choose a leave type.')

    reason = (reason or '').strip()
    if len(reason) < 5:
        raise LeaveError('Give a reason of at least 5 characters.')

    from_date = parse_date(from_date_raw, 'start date')
    to_date = parse_date(to_date_raw, 'end date')
    if to_date < from_date:
        raise LeaveError('The end date cannot be before the start date.')
    span = (to_date - from_date).days + 1
    if span > MAX_LEAVE_DAYS:
        raise LeaveError(
            f'A single application cannot span more than {MAX_LEAVE_DAYS} days.')

    if not faculty.is_faculty:
        raise LeaveError('Only teaching staff can apply for leave.')

    clashes = LeaveModel.overlaps(faculty.id, from_date, to_date)
    if clashes:
        first = clashes[0]
        raise LeaveError(
            f'You already have leave from {first.from_date:%d %b %Y} to '
            f'{first.to_date:%d %b %Y}. Cancel or amend that one first.'
        )

    return LeaveModel.create(faculty.id, chosen, from_date, to_date, reason)


# --------------------------------------------------------------------------
# step 2 -- which periods are actually affected
# --------------------------------------------------------------------------

def candidate_periods_for(faculty_id: int, leave: FacultyLeave) -> List[dict]:
    """Periods the applicant may be missing, one entry per (date, period).

    Built from the courses they are listed to teach, expanded across the leave
    window, and filtered to days the section actually meets. Nothing here claims
    the applicant is missing any of it -- it is what they get to choose from.

    The result is deliberately a superset: a lecturer teaching a shared course is
    listed for every one of its slots, and ticks only their own. The alternative
    -- deriving their slots from an unallocated timetable -- would return nothing
    and leave them unable to apply for leave at all.
    """
    courses = TimetableModel.courses_for_faculty(faculty_id)
    if not courses:
        return []

    wanted_courses = {(row.get('section'), row.get('course_code'))
                      for row in courses}

    # One read of the timetable, narrowed to the courses this person is on.
    periods = []
    for row in TimetableModel.all_periods():
        key = (row.get('section'), row.get('course_code'))
        if key not in wanted_courses:
            continue
        periods.append(row)

    slots_by_section = {}
    offered = []
    for row in sorted(periods, key=lambda r: (r.get('section') or '',
                                               int(r.get('period') or 0))):
        section = row.get('section')
        if section not in slots_by_section:
            slots_by_section[section] = TimetableModel.slots_by_section(section)
        slot = slots_by_section[section].get(int(row.get('period') or 0))
        day = row.get('day')

        cursor = leave.from_date
        while cursor <= leave.to_date:
            if _day_label(cursor) == day:
                offered.append({
                    'date': cursor,
                    'day': day,
                    'period': int(row.get('period') or 0),
                    'section': section,
                    'class_id': row.get('class_id'),
                    'subject_code': row.get('subject_code'),
                    'course_code': row.get('course_code'),
                    'slot_label': _slot_label(slot, row.get('start'),
                                              row.get('end')),
                    # Already arranged for, so shown ticked rather than offered
                    # again.
                    'assigned_faculty_id': row.get('faculty_id'),
                })
            cursor += timedelta(days=1)

    offered.sort(key=lambda o: (o['date'], o['period'], o['section'] or ''))
    return offered


def group_periods(entries: List[dict]) -> List[dict]:
    """Collapse chosen periods into teaching blocks.

    A department's timetable gives a subject a run of consecutive periods -- ES-II
    for T7-Smart Interviews takes four on a Saturday -- and that run is one thing
    to arrange, not four. Grouping them here means the substitute is asked once
    for the block, the HOD approves it once, and there is no reachable state
    where three of the four are covered and the fourth is not.

    Only *consecutive* periods merge. Periods 4, 5 and 7 with 6 belonging to
    something else are two blocks, and the gap has to stay visible: reporting
    "4-7" across a gap would claim cover over a period nobody was ever asked to
    cover.

    Ordering is by date, then period, and each block keeps the section and
    subject identity of the entries it merged -- which is the definition of a
    block here.
    """
    ordered = sorted(entries,
                     key=lambda e: (e['date'], int(e['period'] or 0),
                                    e.get('section') or ''))
    blocks: List[dict] = []
    for entry in ordered:
        if blocks:
            current = blocks[-1]
            same_class = (
                current['date'] == entry['date']
                and current.get('section') == entry.get('section')
                and current.get('subject_code') == entry.get('subject_code')
                and current.get('course_code') == entry.get('course_code')
                and current.get('class_id') == entry.get('class_id')
                and current['_periods'][-1] + 1 == int(entry['period'] or 0))
            if same_class:
                current['_periods'].append(int(entry['period'] or 0))
                continue
        block = dict(entry)
        block['_periods'] = [int(entry['period'] or 0)]
        blocks.append(block)
    return blocks


def confirm_affected(leave: FacultyLeave, faculty, selected) -> List[ClassAdjustment]:
    """Record the periods the applicant says they are missing.

    `selected` is a list of "date|period" pairs, which is what the form posts.
    It is checked against what the applicant was actually *offered* rather than
    taken at face value: a hand-rolled POST naming somebody else's section would
    otherwise create an adjustment for a class this faculty does not teach, and
    the HOD would be asked to approve a substitution that never needed arranging.

    What they confirm is still one tick per period -- the applicant's own view of
    which slots they will miss, and a run of four periods is four things they
    know about. Grouping happens after, when writing the record.
    """
    offered = candidate_periods_for(faculty.id, leave)
    by_key = {}
    for entry in offered:
        by_key[_selection_key(entry['date'], entry['period'])] = entry

    chosen, unknown = [], []
    for raw in selected or []:
        key = _normalise_selection(raw)
        if not key:
            continue
        if key in by_key:
            chosen.append(by_key[key])
        else:
            unknown.append(raw)

    if unknown:
        raise LeaveError(
            'Some of the periods selected are not on your timetable, so they '
            'were ignored. Reload the page and try again.'
        )
    if not chosen and offered:
        raise LeaveError('Select at least one period you will miss.')
    # No periods offered and none selected is not a mistake: it is a leave that
    # touches no class. Refusing here would strand anybody whose courses do not
    # meet inside the window -- they would have no periods to tick, be told they
    # had selected none, and have no way to reach the approval stage at all.

    keep = set()
    for block in group_periods(chosen):
        doc_id = AdjustmentModel.upsert(
            leave_id=leave.id,
            original_faculty_id=faculty.id,
            day=block['date'],
            periods=block['_periods'],
            section=block['section'],
            class_id=block['class_id'],
            subject_code=block['subject_code'],
            course_code=block['course_code'],
        )
        keep.add(doc_id)

    AdjustmentModel.remove(leave.id, keep)

    adjustments = AdjustmentModel.find_by_leave(leave.id)
    LeaveSummary.attach(leave, adjustments)
    LeaveModel.set_adjustment_summary(leave.id, leave.adjustment_status)
    LeaveModel.update_status(leave.id, LeaveStatus.ADJUSTMENT_PENDING)
    leave.status = LeaveStatus.ADJUSTMENT_PENDING
    return adjustments


def mark_uncovered(adjustment_id: str, applicant=None) -> None:
    """Flag a period the applicant cannot find anyone for.

    Not a silent pass. The row becomes UNCOVERED, which keeps it visible on the
    HOD's page as an outstanding class, and which counts towards the override the
    plan allows rather than quietly disappearing from the totals.

    An accepted substitution cannot be marked uncovered: the class *is* covered,
    and un-accepting it would put a colleague's agreed work back in doubt.
    """
    adjustment = AdjustmentModel.find_by_id(adjustment_id)
    if adjustment is None:
        raise LeaveError('That class block is no longer on the application.')
    if adjustment.status == AdjustmentStatus.ACCEPTED:
        raise LeaveError(
            'This block is already covered by an accepted substitution.')
    if applicant is not None and adjustment.original_faculty_id != applicant.id:
        raise LeaveError('That class block is not on your application.')

    AdjustmentModel.set_status(adjustment_id, AdjustmentStatus.UNCOVERED)
    _refresh_leave(adjustment.leave_id)


# --------------------------------------------------------------------------
# step 3 -- who could take it
# --------------------------------------------------------------------------

def suggest_faculty(adjustment: ClassAdjustment, viewer,
                    limit: int = SUGGESTION_LIMIT) -> dict:
    """Rank the lecturers who could take this teaching block.

    Returns ``{'candidates': [...], 'confidence': ..., 'coverage': ...,
    'reason': ...}`` rather than a bare list, because how much to trust the list
    depends on how much of the department's timetable actually has an owner, and
    the interface has to be able to say that.

    Eligibility is not a score, it is a gate: an inactive account, somebody who is
    already on leave, and somebody teaching that slot cannot be suggested at all.
    Presenting them low in a list would invite a request that is certain to fail.

    Everything a candidate needs is gathered in a fixed number of reads *before*
    the loop. The obvious version of this -- asking the store about each
    candidate as it is considered -- costs two queries per person, so one page of
    shortlists for a 200-strong department is several hundred round trips and
    exhausts the project's daily Firestore quota from a single page view. The
    ranking is therefore built from four bulk reads and pure Python.
    """
    coverage = TimetableModel.allocation_coverage()
    busy = TimetableModel.allocated_by_slot()

    # Who could conceivably teach this. The course's own team is the sensible
    # pool, but it is one to three people and may include somebody on leave, so
    # the department widens out from there rather than showing a shortlist of one.
    faculty_ids = set(adjustment.eligible_faculty_ids or [])
    faculty_ids.discard(adjustment.original_faculty_id)

    department = getattr(viewer, 'department', 'CSE')
    # Everyone who teaches, HODs included: the HOD can take a period and can
    # approve one, so they are a legitimate candidate even when the course's
    # teaching team does not list them.
    staff = [p for p in UserModel.get_faculty()
             if p.id != adjustment.original_faculty_id]

    # Who is away that day, over the whole department. Memoised because the
    # shortlist for each affected period asks the same question and the answer
    # cannot change while the page is being rendered.
    away = _memo(
        ('away', adjustment.date, adjustment.date),
        lambda: LeaveModel.find_away_between(
            [p.id for p in staff], adjustment.date, adjustment.date))

    taken = set()
    # Every period in the block, not just the first. A colleague free at 4 but
    # teaching at 5 cannot take a 4-7 block, and offering them the first period
    # alone is how a block ends up half covered.
    for period in adjustment.period_list:
        taken |= busy.get((adjustment.section, adjustment.day, period), set())
    # Somebody holding an accepted substitution for any of these exact periods is
    # already committed to it, whoever the recorded owner is.
    taken |= _accepted_for_block(adjustment)

    # Both of the next two are per-candidate facts, so they are collected for
    # everybody at once rather than looked up inside the loop.
    already_offered = _pending_offer_pairs()
    loads = _workload_counts(adjustment.date)

    ranked = []
    # With little of the timetable allocated, "free" cannot be asserted for
    # anybody -- the absence of a record is not evidence of availability. The
    # wording changes rather than the ranking, because eligibility is still
    # knowable: whoever is not on leave and is not recorded against the slot is
    # genuinely a person worth asking. What is not knowable is whether they are
    # actually in that room at that hour.
    known = coverage['coverage'] >= LOW_CONFIDENCE_COVERAGE
    count = adjustment.period_count

    for person in staff:
        if not person.is_active:
            continue
        if person.id in taken:
            continue
        if adjustment.date in away.get(person.id, set()):
            continue
        # Offering a period to somebody already answering for it is noise.
        if (person.id, adjustment.id) in already_offered:
            continue

        same_course = person.id in faculty_ids
        score = SCORE_AVAILABLE
        reasons = [f'Free for all {count} periods' if count > 1
                   else 'Free at this period'] if known else ['Not recorded as busy']

        if getattr(person, 'department', None) == department:
            score += SCORE_SAME_DEPARTMENT
            reasons.append('Same department')
        if same_course:
            score += SCORE_SAME_COURSE
            reasons.append('Teaches this course')

        load = loads.get(person.id, 0)
        bonus = max(0, WORKLOAD_BONUS_MAX - load * WORKLOAD_BONUS_DIVISOR)
        score += bonus
        reasons.append(f'{load} accepted substitution'
                       f'{"" if load == 1 else "s"}' if load else 'No substitutions yet')

        ranked.append({
            'faculty_id': person.id,
            'name': person.name,
            'employee_code': person.employee_code,
            'designation': person.designation,
            'score': score,
            'same_course': same_course,
            'same_department': getattr(person, 'department', None) == department,
            'workload': load,
            'reasons': reasons,
        })

    # Best score, then least loaded, then by name so the order is stable and two
    # people looking at the same page see the same list.
    ranked.sort(key=lambda c: (-c['score'], c['workload'], c['name'] or ''))

    return {
        'candidates': ranked[:limit],
        'total_candidates': len(ranked),
        'coverage': coverage['coverage'],
        'confidence': ('high' if coverage['coverage'] >= LOW_CONFIDENCE_COVERAGE
                       else 'provisional'),
        'reason': _confidence_note(coverage),
    }


def request_substitution(adjustment_id: str, applicant, faculty_id: int) -> None:
    """Offer one teaching block to one colleague.

    Two refusals, both deliberate. The person must be a real active member of
    staff, and must not already be holding it -- a second offer would let the
    same period be chased by two applicants at once, and the accepting colleague
    would not know which leave they were covering.
    """
    adjustment = AdjustmentModel.find_by_id(adjustment_id)
    if adjustment is None:
        raise LeaveError('That period is no longer on the application.')

    if adjustment.assigned_faculty_id == faculty_id:
        raise LeaveError('You have already offered this block to them.')

    target = UserModel.find_by_id(faculty_id)
    if target is None or not target.is_active:
        raise LeaveError('That lecturer is no longer available.')
    if not target.is_faculty:
        raise LeaveError('Only teaching staff can take a class adjustment.')
    if target.id == adjustment.original_faculty_id:
        raise LeaveError('You cannot cover your own class block.')

    busy = TimetableModel.allocated_by_slot()
    clash = next(
        (p for p in adjustment.period_list
         if target.id in busy.get(
             (adjustment.section, adjustment.day, p), set())), None)
    if clash is not None:
        # Named after the period that clashes rather than "this period", because
        # in a block of four the person cannot tell which one is meant.
        raise LeaveError(
            f'{target.name} already teaches {adjustment.section} at period '
            f'{clash}.')

    away = LeaveModel.find_away_between(
        [target.id], adjustment.date, adjustment.date)
    if adjustment.date in away.get(target.id, set()):
        raise LeaveError(f'{target.name} is on leave that day.')

    AdjustmentModel.set_status(
        adjustment_id, AdjustmentStatus.PENDING, assigned_faculty_id=target.id)
    _refresh_leave(adjustment.leave_id)


def withdraw_substitution(adjustment_id: str, applicant) -> None:
    """Pull back an offer nobody has answered yet.

    Refused once accepted: by then it is a colleague's committed teaching load,
    and cancelling it silently would leave them marked down for work they never
    did. Reassigning is a request to somebody else instead.
    """
    adjustment = AdjustmentModel.find_by_id(adjustment_id)
    if adjustment is None:
        raise LeaveError('That period is no longer on the application.')
    if adjustment.status == AdjustmentStatus.ACCEPTED:
        raise LeaveError(
            'This substitution has been accepted, so it can no longer be '
            'withdrawn. Ask your HOD to reassign it.'
        )
    AdjustmentModel.set_status(adjustment_id, AdjustmentStatus.CANCELLED)
    _refresh_leave(adjustment.leave_id)


def respond_to_substitution(adjustment_id: str, faculty,
                            accept: bool, remarks: str = '') -> None:
    """A colleague's answer to an offer for a teaching block."""
    adjustment = AdjustmentModel.find_by_id(adjustment_id)
    if adjustment is None:
        raise LeaveError('That request no longer exists.')
    if adjustment.assigned_faculty_id != faculty.id:
        raise LeaveError('That request was not sent to you.')
    if adjustment.status != AdjustmentStatus.PENDING:
        raise LeaveError('You have already answered that request.')

    if accept:
        busy = TimetableModel.allocated_by_slot()
        clash = next(
            (p for p in adjustment.period_list
             if faculty.id in busy.get(
                 (adjustment.section, adjustment.day, p), set())), None)
        if clash is not None:
            # Checked again at answer time, not only at offer time: the person may
            # have picked the block up on their own timetable in between, and a
            # clash discovered afterwards is the expensive one.
            raise LeaveError(
                f'Your own timetable now shows you teaching period {clash}, so '
                f'this block cannot be accepted. Ask the HOD to resolve the '
                f'clash.'
            )
        AdjustmentModel.set_status(adjustment_id, AdjustmentStatus.ACCEPTED,
                                   remarks=remarks)
    else:
        if len((remarks or '').strip()) < 3:
            raise LeaveError('Add a short note so they can pick somebody else.')
        AdjustmentModel.set_status(adjustment_id, AdjustmentStatus.REJECTED,
                                   remarks=remarks)
    _refresh_leave(adjustment.leave_id)


# --------------------------------------------------------------------------
# step 4 -- hand it to the HOD
# --------------------------------------------------------------------------

def submit_to_hod(leave_id: int, applicant) -> FacultyLeave:
    """Move a fully-arranged application in front of the HOD.

    Refused while any period is still an open offer, because the HOD is deciding
    whether the teaching is covered and an unanswered request is not coverage.
    `UNCOVERED` does not block: that is the applicant explicitly flagging a class
    they could not staff, which the plan says the HOD may approve over.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None:
        raise LeaveError('That application no longer exists.')
    if leave.faculty_id != applicant.id:
        raise LeaveError('That application is not yours.')
    if leave.status not in SUBMITTABLE_STATUSES:
        raise LeaveError('This application has already been sent for approval.')

    adjustments = AdjustmentModel.find_by_leave(leave_id)
    LeaveSummary.attach(leave, adjustments)

    open_offers = [a for a in adjustments if a.status == AdjustmentStatus.PENDING]
    if open_offers:
        raise LeaveError(
            f'{len(open_offers)} period'
            f'{"" if len(open_offers) == 1 else "s"} still awaiting an answer. '
            f'Wait for a reply, or mark '
            f'{"" if len(open_offers) == 1 else "them"} uncovered for the HOD.'
        )

    # A leave still in DRAFT has had no period confirmed, which is only allowed
    # when there is nothing to confirm. Otherwise the applicant is skipping the
    # step that finds their affected classes.
    if leave.status == LeaveStatus.DRAFT and adjustments:
        raise LeaveError(
            'Confirm which periods you are missing before sending this for '
            'approval.'
        )

    uncovered = [a for a in adjustments
                 if a.status == AdjustmentStatus.UNCOVERED]
    if uncovered:
        logging.getLogger(__name__).warning(
            'Leave %s sent with %d uncovered period(s)', leave_id,
            len(uncovered))

    LeaveModel.update_status(leave_id, _next_approver_stage(applicant))
    # Re-read rather than returning the copy loaded above: that one still carries
    # ADJUSTMENT_PENDING, and a caller checking `leave.status` would conclude the
    # application never reached an approver.
    return LeaveModel.find_by_id(leave_id)


def _next_approver_stage(applicant) -> LeaveStatus:
    """Which queue this application belongs in, given who is applying.

    Almost everyone goes to the HOD. An HOD does not, because no HOD may decide
    another HOD's leave -- `hod_act_on_leave` refuses self-approval, and a
    hand-written URL cannot get round that -- so their application goes straight
    to the Dean. Filing it under HOD_PENDING would put it in the one queue the
    applicant cannot empty, where it would sit forever.

    The applicant's role is read here rather than assumed, because the same
    person can hold both jobs: an HOD in this department teaches as well as
    administering, and it is the approving hat that skips a stage.
    """
    if getattr(applicant, 'role', None) == UserRole.HOD:
        return LeaveStatus.DEAN_PENDING
    return LeaveStatus.HOD_PENDING


def preview_leave_edit(leave_id: int, applicant, leave_type_raw: str,
                       from_raw: str, to_raw: str, reason_raw: str) -> dict:
    """Check a proposed amendment and describe what it would change.

    Returns ``{'leave', 'values', 'changes'}``. Nothing is written -- this is the
    step between the form and the confirmation, and it is where every rule that
    also applies to a new application is enforced again.

    The rules are re-run rather than assumed, because an edit is not a smaller
    version of a create: moving the dates past another application of the same
    person's, or stretching the span past the limit, would both create a leave
    the create path would have refused.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None:
        raise LeaveError('That application no longer exists.')
    if leave.faculty_id != applicant.id:
        raise LeaveError('That application is not yours.')

    leave_type = _coerce_type(leave_type_raw)
    if leave_type is None:
        raise LeaveError('Choose a leave type.')

    from_date = parse_date(from_raw, 'start date')
    to_date = parse_date(to_raw, 'end date')
    if to_date < from_date:
        raise LeaveError('The end date cannot be before the start date.')
    span = (to_date - from_date).days + 1
    if span > MAX_LEAVE_DAYS:
        raise LeaveError(
            f'A single application cannot span more than {MAX_LEAVE_DAYS} days.')

    reason = (reason_raw or '').strip()
    if len(reason) < 5:
        raise LeaveError('Give a reason of at least a few words.')

    # Overlap is checked against this person's other live applications, and the
    # exclusion is by id rather than by date so that re-saving an edit which did
    # not move the dates is never reported as colliding with itself.
    for other in LeaveModel.find_open(applicant.id, exclude_id=leave_id):
        if other.from_date and other.to_date \
                and other.from_date <= to_date and from_date <= other.to_date:
            raise LeaveError(
                f'Those dates overlap another open application, from '
                f'{other.from_date:%d %b %Y} to {other.to_date:%d %b %Y}. '
                f'Cancel that one first.'
            )

    values = {
        'leave_type': leave_type.value,
        'from_date': from_date,
        'to_date': to_date,
        'reason': reason,
    }
    changes = diff_fields(
        {
            'leave_type': leave.leave_type.value,
            'from_date': leave.from_date.isoformat() if leave.from_date else '',
            'to_date': leave.to_date.isoformat() if leave.to_date else '',
            'reason': leave.reason,
        },
        {
            'leave_type': leave_type.value,
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
            'reason': reason,
        },
        LEAVE_EDIT_LABELS,
    )
    return {'leave': leave, 'values': values, 'changes': changes,
            'leave_type': leave_type, 'from_date': from_date,
            'to_date': to_date, 'reason': reason}


def apply_leave_edit(leave_id: int, applicant, leave_type_raw: str,
                     from_raw: str, to_raw: str, reason_raw: str) -> dict:
    """Save a confirmed amendment, and record what it changed.

    The validation runs again here rather than trusting the confirmation page:
    between showing it and pressing the button the record could have been
    changed by the applicant in another tab, and a preview that no longer matches
    what would be saved is worse than no preview.

    Editing is allowed after submission, and every amendment is written to the
    history. `status` is not touched, so amending dates the HOD is looking at
    does not quietly withdraw the application -- the HOD sees the original and
    the change, and decides on the current one.
    """
    proposed = preview_leave_edit(leave_id, applicant, leave_type_raw,
                                  from_raw, to_raw, reason_raw)
    changes = proposed['changes']
    if not changes:
        return proposed

    leave = proposed['leave']
    was_submitted = leave.status != LeaveStatus.DRAFT

    LeaveModel.update_details(
        leave_id, proposed['leave_type'], proposed['from_date'],
        proposed['to_date'], proposed['reason'])

    # Periods recorded for dates the application no longer covers are dropped.
    # Leaving them would put a substitute on a class for a day the applicant is
    # not away, which is the kind of error nobody notices until the register does.
    if any(c.key in ('from_date', 'to_date') for c in changes):
        _drop_periods_outside(leave_id, proposed['from_date'],
                              proposed['to_date'])

    if was_submitted:
        store.insert(AMENDMENTS, {
            'leave_id': leave_id,
            'actor_id': applicant.id,
            'at': datetime.now(),
            'changes': [c.as_dict() for c in changes],
        })

    proposed['leave'] = LeaveModel.find_by_id(leave_id)
    proposed['recorded'] = was_submitted
    return proposed


def _drop_periods_outside(leave_id: int, from_date: date, to_date: date) -> int:
    """Delete recorded periods that fall outside a narrowed date range."""
    removed = 0
    for adjustment in AdjustmentModel.find_by_leave(leave_id):
        if adjustment.date and not (from_date <= adjustment.date <= to_date):
            store.delete(ADJUSTMENTS, adjustment.id)
            removed += 1
    if removed:
        LeaveSummary.attach(
            LeaveModel.find_by_id(leave_id),
            AdjustmentModel.find_by_leave(leave_id))
    return removed


def _coerce_type(raw):
    for kind in LeaveType:
        if kind.value == (raw or '').strip():
            return kind
    return None


def cancel_leave(leave_id: int, applicant) -> None:
    """Withdraw an application before an approver decides it.

    Once it is with the HOD or the Dean it is a departmental record, and only one
    of them can end it -- otherwise an applicant could retract a leave the
    department had already acted on.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None or leave.faculty_id != applicant.id:
        raise LeaveError('That application no longer exists.')
    if leave.status in (LeaveStatus.HOD_PENDING, LeaveStatus.DEAN_PENDING,
                        LeaveStatus.FINAL_APPROVED):
        raise LeaveError(
            'This application has been sent for approval and can no longer be '
            'withdrawn by you.')
    LeaveModel.update_status(leave_id, LeaveStatus.CANCELLED)
    for adjustment in AdjustmentModel.find_by_leave(leave_id):
        if adjustment.status == AdjustmentStatus.PENDING:
            AdjustmentModel.set_status(adjustment.id,
                                       AdjustmentStatus.CANCELLED)


def hod_act_on_leave(leave_id: int, hod, approve: bool, remarks: str) -> FacultyLeave:
    """The HOD's decision on a fully-arranged application.

    Two things are recorded that a bare status change would lose: who decided and
    when, and any periods the applicant could not staff. The second is the reason
    an uncovered class is surfaced here at all -- an approval over one is a
    decision somebody has to be able to see afterwards.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None:
        raise LeaveError('That application no longer exists.')
    if leave.status != LeaveStatus.HOD_PENDING:
        raise LeaveError('That application is not waiting on the HOD.')
    if leave.faculty_id == hod.id:
        # An HOD teaches here, so they can file leave like anyone else -- and that
        # own application comes back into the queue they are looking at. Checked
        # in the service rather than only by hiding the row, because this is the
        # one check a hand-written URL to /hod/leave/<id>/action would skip, and
        # an HOD approving their own leave is exactly the thing that has to be
        # impossible rather than merely hidden.
        #
        # `submit_to_hod` routes an HOD's own application straight to the Dean, so
        # this should be unreachable. It stays as the backstop for a record that
        # reached this state by some other route.
        raise LeaveError(
            'This is your own application, so it is decided by the Dean rather '
            'than by you.')

    if approve and len((remarks or '').strip()) < 5 and \
            LeaveSummary.attach(leave, AdjustmentModel.find_by_leave(leave_id)) \
            .uncovered_count:
        raise LeaveError(
            'Say how the uncovered class will be handled before approving.')

    LeaveModel.update_status(
        leave_id,
        LeaveStatus.DEAN_PENDING if approve else LeaveStatus.REJECTED,
        hod_by=hod.id,
        # Recorded rather than inferred. The letter has to be able to say who
        # decided and when, months later, and `updated_at` is the time of the last
        # write of any kind -- which is not the same moment once anything else
        # touches the record.
        hod_at=datetime.now(),
        hod_remarks=(remarks or '').strip() or None,
    )
    return LeaveModel.find_by_id(leave_id)


def dean_act_on_leave(leave_id: int, dean, approve: bool,
                      remarks: str) -> FacultyLeave:
    """The Dean's decision -- the last signature on a leave.

    Approving moves the application to FINAL_APPROVED, which is the only state a
    leave letter may be printed from, and is what actually takes the applicant
    out of teaching.

    The same three things are recorded as for the HOD, and for the same reasons:
    who decided, when, and in what words. `updated_at` cannot stand in for the
    second of those, because it is the time of the last write of any kind and the
    record is written to again after an approval is recorded.
    """
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None:
        raise LeaveError('That application no longer exists.')
    if leave.status != LeaveStatus.DEAN_PENDING:
        raise LeaveError(
            'That application is not waiting on the Dean. It may already have '
            'been decided.')

    # A Dean does not file leave in this portal -- `is_faculty` excludes the role
    # precisely so this cannot arise -- but the refusal is here anyway, for the
    # same reason the HOD's is: a hand-written POST to this endpoint must not be
    # able to do something the interface will not offer.
    if leave.faculty_id == dean.id:
        raise LeaveError('This is your own application.')

    trimmed = (remarks or '').strip()
    if approve and len(trimmed) < 5:
        # A bare "approved" leaves nobody able to say, months later, on what
        # basis the leave was granted. The HOD's remark covers the teaching; this
        # one is the grant itself, so it is required unconditionally.
        raise LeaveError('Write a remark before approving.')

    LeaveModel.update_status(
        leave_id,
        LeaveStatus.FINAL_APPROVED if approve else LeaveStatus.REJECTED,
        dean_by=dean.id,
        dean_at=datetime.now(),
        dean_remarks=trimmed or None,
    )
    return LeaveModel.find_by_id(leave_id)


def letter_context(leave: FacultyLeave) -> dict:
    """Everything the leave letter needs, from one place.

    Three roles print this letter -- the applicant, the HOD who arranged the
    teaching, and the Dean who granted it -- so the document is built once here
    rather than three times in three routes. If the wording changes it changes
    everywhere, which is the only way a letter that has already been printed can
    still be read as the truth about this record.

    Only ever called for a FINAL_APPROVED leave. Every caller checks that first;
    this raises rather than rendering a letter for an application that has not
    been granted.
    """
    if not leave.is_final:
        raise LeaveError(
            'This application has not been approved yet, so there is no letter '
            'to print.')

    from app.utils.qr import leave_letter_qr

    applicant = UserModel.find_by_id(leave.faculty_id)
    hod = UserModel.find_by_id(leave.hod_by) if leave.hod_by else None
    dean = UserModel.find_by_id(leave.dean_by) if leave.dean_by else None
    adjustments = AdjustmentModel.find_by_leave(leave.id)
    LeaveSummary.attach(leave, adjustments)

    substitutes = {}
    for adjustment in adjustments:
        if adjustment.assigned_faculty_id:
            person = UserModel.find_by_id(adjustment.assigned_faculty_id)
            if person:
                substitutes[adjustment.assigned_faculty_id] = person.name

    # A failing encoder degrades to a letter without a code rather than to a
    # letter that will not print. The reference is built by hand here so the
    # number in the table is still right when that happens.
    try:
        qr = leave_letter_qr(leave.id)
    except Exception:
        logging.getLogger(__name__).exception(
            'Could not build the leave letter QR code')
        qr = {'url': '', 'image': '', 'reference': f'LVR-{leave.id:04d}'}

    return {
        'leave': leave,
        'applicant': applicant,
        'hod': hod,
        'dean': dean,
        'adjustments': adjustments,
        'substitutes': substitutes,
        'qr': qr,
    }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _selection_key(day: date, period: int) -> str:
    return f'{day.isoformat()}|{period}'


def _normalise_selection(raw) -> str:
    """A posted "date|period" into the same shape as a key."""
    text = str(raw or '').strip()
    if '|' not in text:
        return ''
    day, _, period = text.partition('|')
    try:
        from datetime import datetime
        parsed = datetime.strptime(day.strip(), '%Y-%m-%d').date()
        return _selection_key(parsed, int(period))
    except (ValueError, TypeError):
        return ''


def _slot_label(slot, start: str, end: str) -> str:
    """A period's clock times, preferring what this section actually uses."""
    if start and end:
        return f'{start} - {end}'
    if slot is not None and slot.start:
        return f'{slot.start} - {slot.end or ""}'.strip(' -')
    return ''


def _memo(key, loader):
    """A value computed once per request.

    `loader` runs at most once per request for a given key; after that the memo is
    served from the request context. Outside a request context -- a migration
    script, a test -- there is nothing to memoise against, so it just runs.

    This exists because the leave page asks the same three questions about the
    adjustment collection once per affected period. Without it a leave covering
    seven periods pays for seven of each, and Firestore's free daily quota is
    small enough that one page view can exhaust a project's for the day.
    """
    from flask import g, has_request_context

    if not has_request_context():
        return loader()

    cache = getattr(g, '_au_leave_cache', None)
    if cache is None:
        cache = {}
        g._au_leave_cache = cache
    if key not in cache:
        cache[key] = loader()
    return cache[key]


def _accepted_for_block(adjustment: ClassAdjustment) -> set:
    """Who has already accepted any period of this block, whoever was asked.

    Queried by date and section and then filtered on the row's own period list,
    rather than by the stored `period`. Since blocks landed, `period` holds only
    the first member, so a store filter on it would miss a colleague who accepted
    the 5 in a 4-7 block and would offer them the same teaching twice.
    """
    key = ('accepted_block', adjustment.section, adjustment.day,
           adjustment.date, tuple(adjustment.period_list))

    def load():
        holders = set()
        wanted = set(adjustment.period_list)
        for row in store.documents(ADJUSTMENTS, date=adjustment.date,
                                   section=adjustment.section,
                                   status=AdjustmentStatus.ACCEPTED.value):
            if not row.get('assigned_faculty_id'):
                continue
            listed = [int(p) for p in (row.get('periods') or []) if p]
            # A row written before blocks existed carries only `period`.
            overlaps = wanted & (set(listed) or {int(row.get('period') or 0)})
            if overlaps:
                holders.add(row['assigned_faculty_id'])
        return holders

    return _memo(key, load) - {adjustment.original_faculty_id}


def _pending_offer_pairs() -> set:
    """Every ``(faculty_id, adjustment_id)`` currently awaiting an answer.

    One read for the whole department rather than one per candidate. The pair is
    carried as a set because the question "is this person already answering for
    this exact period" is asked about everybody, and answering it individually
    turns a single shortlist into a query per member of staff.
    """
    def load():
        pairs = set()
        for row in store.documents(ADJUSTMENTS,
                                   status=AdjustmentStatus.PENDING.value):
            assigned = row.get('assigned_faculty_id')
            if assigned and row.get('id'):
                pairs.add((assigned, row['id']))
        return pairs

    return _memo('pending_offers', load)


def _workload_counts(day: date) -> dict:
    """Accepted substitutions per faculty on one day, from a single read.

    Counting in Python over every accepted substitution beats asking per
    lecturer: the collection is small, and the alternative is a query per
    candidate on the page.
    """
    def load():
        counts = {}
        for row in store.documents(ADJUSTMENTS,
                                   status=AdjustmentStatus.ACCEPTED.value):
            if row.get('date') != day:
                continue
            assigned = row.get('assigned_faculty_id')
            if assigned:
                counts[assigned] = counts.get(assigned, 0) + 1
        return counts

    return _memo(('workload', day), load)


def _refresh_leave(leave_id: int) -> None:
    """Recompute a leave's summary after any adjustment changes."""
    leave = LeaveModel.find_by_id(leave_id)
    if leave is None or leave.status != LeaveStatus.ADJUSTMENT_PENDING:
        return
    adjustments = AdjustmentModel.find_by_leave(leave_id)
    LeaveSummary.attach(leave, adjustments)
    LeaveModel.set_adjustment_summary(leave_id, leave.adjustment_status)
    if leave.adjustments_resolved:
        LeaveModel.update_status(leave_id, LeaveStatus.ADJUSTMENT_COMPLETE)


def _confidence_note(coverage: dict) -> str:
    if coverage['coverage'] >= LOW_CONFIDENCE_COVERAGE:
        return ''
    return (
        f"{coverage['missing']} of {coverage['total']} periods have no lecturer "
        f"recorded against them, so somebody who looks free may in fact be "
        f"teaching that slot. Treat this as a shortlist to try, not a list of "
        f"who is definitely available."
    )
