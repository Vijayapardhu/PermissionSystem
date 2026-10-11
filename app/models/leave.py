"""Faculty leave, class adjustments, and the timetable they are worked out from.

Three collections sit alongside the student permission flow:

``timetable_periods``   written by ``migrations/import_college_data.py``. One row
                        per section, day and period: when the section meets, for
                        which course, and who is meant to teach it.
``faculty_leaves``      one application, and its approval state.
``leave_adjustments``   one row per period that needs a substitute.

**On `timetable_periods.faculty_id`.** The college's published timetable names a
*pool* of one to three faculty per course and never says who teaches which slot,
so the import leaves that field null rather than guessing. Two consequences are
load-bearing here:

- A period with no owner cannot tell a free lecturer from a busy one, so the
  availability search below is only as good as the allocation the department has
  entered. ``allocation_coverage()`` measures that, and the service refuses to
  present a confident answer when it is too low.
- The applicant is the one person who *does* know which of their slots they are
  missing, so ``candidate_periods_for`` offers them exactly their own periods to
  confirm. That both resolves the allocation for their own timetable and produces
  the affected-class list, without waiting on an administrator.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional

from app.models import (
    AdjustmentStatus, ClassAdjustment, FacultyLeave, LeaveStatus, LeaveType,
)
from app.models.firestore import store

LEAVES = 'faculty_leaves'
ADJUSTMENTS = 'leave_adjustments'
#: Amendments made after an application was submitted. Kept apart from the leave
#: itself so the record the HOD approves is the record that was applied, and the
#: trail of what was altered afterwards is something you can choose not to read.
AMENDMENTS = 'leave_amendments'
PERIODS = 'timetable_periods'
COURSES = 'section_courses'
SLOTS = 'timetable_slots'
GROUPS = 'class_groups'

#: Weekday labels, matching the timetable grid. Indexed by `date.weekday()`.
DAY_NAMES = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')


@dataclass
class TimetableSlot:
    period: int
    start: Optional[str]
    end: Optional[str]


def _day_label(day: date) -> str:
    """A date's weekday in the grid's own spelling ('Mon').

    Sunday maps to 'SUN', which no published section uses, so it falls out of
    every lookup and a Sunday in a leave range contributes no periods.
    """
    return DAY_NAMES[day.weekday()].title()[:3]


def _adjustment_id(leave_id: int, day: date, section: str, subject_code) -> str:
    """Natural key for one teaching block.

    Keyed on the date, the section and the subject rather than on the period,
    which is what makes two periods of the same subject on the same day the same
    row instead of two rows to arrange cover for separately.

    Deterministic for the same reason as before: re-running detection after the
    applicant edits their selection updates the row rather than accumulating
    duplicates -- and it is why other records use composed ids too.

    Readable rather than hashed, a slug of the section and subject, because these
    ids turn up in the store and in bug reports. Non-alphanumerics collapse to
    `_`, since a section label carries spaces and hyphens.
    """
    label = f'{section or "?"}-{subject_code or "?"}'
    slug = ''.join(ch if ch.isalnum() else '_' for ch in label)
    return f'{leave_id}:{day.isoformat()}:{slug}'


class LeaveModel:
    @staticmethod
    def create(faculty_id: int, leave_type: LeaveType, from_date: date,
               to_date: date, reason: str) -> FacultyLeave:
        row = store.insert(LEAVES, {
            'faculty_id': faculty_id,
            'leave_type': leave_type.value,
            'from_date': from_date,
            'to_date': to_date,
            'days': (to_date - from_date).days + 1,
            'reason': reason,
            'status': LeaveStatus.DRAFT.value,
            'adjustment_status': AdjustmentStatus.PENDING.value,
        })
        return LeaveModel._to_leave(row)

    @staticmethod
    def find_by_id(leave_id: int) -> Optional[FacultyLeave]:
        row = store.get(LEAVES, leave_id)
        return LeaveModel._to_leave(row) if row else None

    @staticmethod
    def find_for_faculty(faculty_id: int, statuses=None,
                         limit: int = 50) -> List[FacultyLeave]:
        rows = store.documents(LEAVES, faculty_id=faculty_id)
        wanted = {s.value for s in statuses} if statuses else None
        leaves = [LeaveModel._to_leave(r) for r in rows]
        if wanted:
            leaves = [l for l in leaves if l.status.value in wanted]
        leaves.sort(key=lambda l: (l.from_date, -int(l.id or 0)), reverse=True)
        return leaves[:limit]

    @staticmethod
    def find_by_status(status: LeaveStatus, limit: int = 100) -> List[FacultyLeave]:
        """Every leave in one state -- the approver's queue.

        Filtered in the query, not in Python. Unlike the availability search this
        is a plain single-field equality, which Firestore serves from an
        automatic index and SQL from `doc_index`, so there is no reason to read
        the whole collection and discard most of it. That distinction matters:
        this is the one list on the page that is the approver's actual to-do.

        Oldest first by the date the leave starts, because a request that has been
        sitting since last month is the one that has been waiting longest in any
        sense a person would recognise.
        """
        rows = store.documents(LEAVES, status=status.value)
        leaves = [LeaveModel._to_leave(r) for r in rows]
        leaves.sort(key=lambda l: (l.from_date or date.max, int(l.id or 0)))
        return leaves[:limit]

    @staticmethod
    def find_open(faculty_id: int, exclude_id: int = None) -> List[FacultyLeave]:
        """This faculty's live leaves, for the duplicate check.

        Filtered on ``faculty_id`` in the query rather than in Python. Reading the
        whole collection and discarding most of it costs a full-collection read
        on every application, which is both a bill and -- once the department has
        a few thousand rows -- a request that starts timing out.

        The status is still narrowed in Python rather than in the query: a status
        filter alongside a second equality would need a composite index created in
        the console before it works, and a request that errors until someone does
        that is a worse failure than reading one faculty's rows.
        """
        dead = {s.value for s in LeaveStatus if s in (
            LeaveStatus.REJECTED, LeaveStatus.CANCELLED)}
        rows = store.documents(LEAVES, faculty_id=faculty_id)
        live = []
        for row in rows:
            if row.get('status') in dead:
                continue
            if exclude_id and row.get('id') == exclude_id:
                continue
            live.append(LeaveModel._to_leave(row))
        return live

    @staticmethod
    def find_away_between(faculty_ids, from_date: date, to_date: date
                          ) -> dict:
        """Which of these faculty are away at any point in a window.

        Returned as a mapping of faculty id to the leave dates they are out, so
        the availability search can exclude them without a second pass.
        """
        wanted = set(faculty_ids or [])
        if not wanted or not from_date or not to_date:
            return {}

        blocking = {LeaveStatus.HOD_PENDING.value,
                    LeaveStatus.DEAN_PENDING.value,
                    LeaveStatus.FINAL_APPROVED.value}

        away = {}
        for row in store.documents(LEAVES):
            if row.get('faculty_id') not in wanted:
                continue
            # Only a leave that has actually been approved takes somebody out of
            # teaching. One still being arranged does not, so its applicant stays
            # available -- and stays findable, which is the whole point of
            # arranging a substitute before approval.
            if row.get('status') not in blocking:
                continue
            start, end = row.get('from_date'), row.get('to_date')
            if not start or not end or end < from_date or start > to_date:
                continue
            days = set()
            cursor = start
            while cursor <= end:
                days.add(cursor)
                cursor += timedelta(days=1)
            away.setdefault(row['faculty_id'], set()).update(days)
        return away

    @staticmethod
    def overlaps(faculty_id: int, from_date: date, to_date: date,
                 exclude_id: int = None) -> List[FacultyLeave]:
        """Live leaves that clash with a proposed window, for one faculty."""
        clashes = []
        for leave in LeaveModel.find_open(faculty_id, exclude_id=exclude_id):
            if leave.from_date <= to_date and leave.to_date >= from_date:
                clashes.append(leave)
        clashes.sort(key=lambda l: (l.from_date, l.to_date))
        return clashes

    @staticmethod
    def update_status(leave_id: int, status: LeaveStatus,
                      **fields) -> bool:
        values = {'status': status.value}
        values.update({k: v for k, v in fields.items() if v is not None})
        return store.update(LEAVES, leave_id, values)

    @staticmethod
    def set_adjustment_summary(leave_id: int, summary: str) -> bool:
        return store.update(LEAVES, leave_id, {'adjustment_status': summary})

    @staticmethod
    def update_details(leave_id: int, leave_type: LeaveType, from_date: date,
                       to_date: date, reason: str) -> bool:
        """Change the four fields the applicant owns.

        `days` is recomputed rather than accepted from the form, because a stale
        day count is the kind of thing that only shows up in a printed letter
        weeks later. `status` is deliberately untouched: amending the dates of a
        leave that is with the HOD does not pull it back to being a draft, and
        the amendment is recorded alongside it instead.
        """
        return store.update(LEAVES, leave_id, {
            'leave_type': leave_type.value,
            'from_date': from_date,
            'to_date': to_date,
            'days': (to_date - from_date).days + 1,
            'reason': (reason or '').strip(),
        })

    @staticmethod
    def amendments(leave_id: int) -> List[dict]:
        """Everything that was changed after the application was submitted.

        Newest last, so the page reads top-to-bottom in the order it happened.
        Each entry carries the actor's name resolved at read time rather than
        copied in at write time: a person who has since been renamed should be
        shown under their current name, and one who has been deactivated should
        still be identifiable.
        """
        from app.models.user import UserModel

        rows = store.documents(AMENDMENTS, leave_id=leave_id)
        entries = []
        for row in rows:
            actor = UserModel.find_by_id(row.get('actor_id'))
            entries.append({
                'id': row.get('id'),
                'actor_id': row.get('actor_id'),
                'actor_name': (actor.name if actor else None),
                'at': row.get('at') or row.get('created_at'),
                'changes': row.get('changes') or [],
            })
        entries.sort(key=lambda e: (e['at'] or datetime.min, e['id'] or 0))
        return entries

    @staticmethod
    def _to_leave(row) -> FacultyLeave:
        return FacultyLeave(
            id=int(row['id']),
            faculty_id=row.get('faculty_id'),
            leave_type=_coerce_leave_type(row.get('leave_type')),
            from_date=row.get('from_date'),
            to_date=row.get('to_date'),
            days=int(row.get('days') or 0),
            reason=row.get('reason') or '',
            status=_coerce_leave_status(row.get('status')),
            adjustment_status=row.get('adjustment_status')
            or AdjustmentStatus.PENDING.value,
            submitted_at=row.get('submitted_at'),
            hod_by=row.get('hod_by'),
            hod_at=row.get('hod_at'),
            hod_remarks=row.get('hod_remarks'),
            dean_by=row.get('dean_by'),
            dean_at=row.get('dean_at'),
            dean_remarks=row.get('dean_remarks'),
            created_at=row.get('created_at'),
            updated_at=row.get('updated_at'),
        )


class AdjustmentModel:
    @staticmethod
    def upsert(leave_id: int, original_faculty_id: int, day: date,
               periods: List[int], section: str, class_id, subject_code,
               course_code) -> str:
        """Record one teaching block, replacing any previous selection.

        `periods` is the whole block, ascending. The stored `period` is its first
        member, kept because it is what the lists are ordered by and what a row
        written before blocks existed still carries.
        """
        block = sorted(set(int(p) for p in periods))
        if not block:
            raise ValueError('a teaching block needs at least one period')

        doc_id = _adjustment_id(leave_id, day, section, subject_code)
        label = _day_label(day)
        existing = store.get(ADJUSTMENTS, doc_id)

        values = {
            'leave_id': leave_id,
            'date': day,
            'day': label,
            'period': block[0],
            'periods': block,
            'section': section,
            'class_id': class_id,
            'subject_code': subject_code,
            'course_code': course_code,
            'original_faculty_id': original_faculty_id,
            'eligible_faculty_ids': _eligible_for(course_code, section),
        }
        if existing:
            # A block the applicant already arranged a substitute for keeps that
            # arrangement; re-confirming the list should not undo their work. The
            # arrangement is only kept when the block is unchanged, though --
            # a colleague who agreed to period 4 has not agreed to 4-7.
            previous = AdjustmentModel._to_adjustment(existing)
            same_block = previous.period_list == block
            values['status'] = existing.get('status') or \
                AdjustmentStatus.PENDING.value
            values['assigned_faculty_id'] = existing.get('assigned_faculty_id') \
                if same_block else None
            values['requested_at'] = existing.get('requested_at') \
                if same_block else None
            values['responded_at'] = existing.get('responded_at') \
                if same_block else None
            values['remarks'] = existing.get('remarks') if same_block else None
            store.update(ADJUSTMENTS, doc_id, values)
        else:
            store.insert(ADJUSTMENTS, dict(
                values,
                status=AdjustmentStatus.PENDING.value,
                assigned_faculty_id=None,
            ), doc_id=doc_id)
        return doc_id

    @staticmethod
    def remove(leave_id: int, keep_ids) -> int:
        """Delete the leave's adjustments that the applicant no longer claims."""
        removed = 0
        for row in store.documents(ADJUSTMENTS, leave_id=leave_id):
            if row.get('id') not in keep_ids:
                # Only an untouched row is safe to drop. One a substitute has
                # already accepted is a promise to a colleague, so it is marked
                # CANCELLED and left visible rather than deleted.
                if row.get('status') in (AdjustmentStatus.PENDING.value, None) \
                        and not row.get('assigned_faculty_id'):
                    store.delete(ADJUSTMENTS, row['id'])
                    removed += 1
                else:
                    store.update(ADJUSTMENTS, row['id'],
                                 {'status': AdjustmentStatus.CANCELLED.value})
        return removed

    @staticmethod
    def find_by_leave(leave_id: int) -> List[ClassAdjustment]:
        rows = store.documents(ADJUSTMENTS, leave_id=leave_id)
        rows = [r for r in rows if r.get('status') != AdjustmentStatus.CANCELLED.value]
        adjustments = [AdjustmentModel._to_adjustment(r) for r in rows]
        adjustments.sort(key=lambda a: (a.date or date.min, a.period or 0))
        return adjustments

    @staticmethod
    def find_by_id(adjustment_id: str) -> Optional[ClassAdjustment]:
        row = store.get(ADJUSTMENTS, adjustment_id)
        return AdjustmentModel._to_adjustment(row) if row else None

    @staticmethod
    def find_pending_for(faculty_id: int) -> List[ClassAdjustment]:
        """Substitution requests awaiting this person's answer, soonest first."""
        rows = store.documents(ADJUSTMENTS, assigned_faculty_id=faculty_id,
                               status=AdjustmentStatus.PENDING.value)
        adjustments = [AdjustmentModel._to_adjustment(r) for r in rows]
        adjustments.sort(key=lambda a: (a.date or date.min, a.period or 0))
        return adjustments

    @staticmethod
    def find_accepted_for(faculty_id: int, limit: int = 50) -> List[ClassAdjustment]:
        """Substitutions this person has already taken on."""
        rows = store.documents(ADJUSTMENTS, assigned_faculty_id=faculty_id,
                               status=AdjustmentStatus.ACCEPTED.value)
        adjustments = [AdjustmentModel._to_adjustment(r) for r in rows]
        adjustments.sort(key=lambda a: (a.date or date.min, a.period or 0))
        return adjustments[:limit]

    @staticmethod
    def set_status(adjustment_id: str, status: AdjustmentStatus,
                   assigned_faculty_id=None, remarks: str = None) -> bool:
        values = {'status': status.value}
        if assigned_faculty_id is not None:
            values['assigned_faculty_id'] = assigned_faculty_id
        values['responded_at'] = datetime.utcnow()
        if remarks:
            values['remarks'] = remarks.strip()
        return store.update(ADJUSTMENTS, adjustment_id, values)

    @staticmethod
    def workload_between(faculty_id: int, from_date: date,
                         to_date: date) -> int:
        """Substitutions this person has already accepted inside a window.

        The tie-breaker in the availability ranking, and the reason it counts
        *accepted* rather than offered: an offer nobody has answered is not
        work, and counting it would make a popular lecturer look unavailable for
        being asked.
        """
        count = 0
        for row in store.documents(ADJUSTMENTS, assigned_faculty_id=faculty_id,
                                   status=AdjustmentStatus.ACCEPTED.value):
            day = row.get('date')
            if day and from_date <= day <= to_date:
                count += 1
        return count

    @staticmethod
    def _to_adjustment(row) -> ClassAdjustment:
        return ClassAdjustment(
            id=row['id'],
            leave_id=row.get('leave_id'),
            date=row.get('date'),
            day=row.get('day') or '',
            period=int(row.get('period') or 0),
            # `periods` is absent on rows written before blocks existed. The
            # fall back to the single period happens in `ClassAdjustment.period_list`,
            # so one place decides what an old row means.
            periods=[int(p) for p in (row.get('periods') or []) if p],
            section=row.get('section') or '',
            class_id=row.get('class_id'),
            subject_code=row.get('subject_code'),
            course_code=row.get('course_code'),
            original_faculty_id=row.get('original_faculty_id'),
            assigned_faculty_id=row.get('assigned_faculty_id'),
            status=_coerce_adjustment_status(row.get('status')),
            requested_at=row.get('requested_at'),
            responded_at=row.get('responded_at'),
            remarks=row.get('remarks'),
            eligible_faculty_ids=list(row.get('eligible_faculty_ids') or []),
        )


class TimetableModel:
    """Reads over the imported timetable, kept in one place.

    Each method is a whole-collection read. Firestore cannot do a composite
    ``where`` on two fields without an index created in the console first, and a
    department's timetable is a few hundred rows -- so the reads are broad and the
    narrowing happens here, which is the same trade the rest of the app makes.

    Broad reads are cheap in latency and expensive in quota, so the whole
    timetable is read **once per request** and shared. ``allocation_coverage``,
    ``allocated_by_slot`` and ``all_periods`` each want the same 250 rows, and the
    leave page asks for all three once per affected period; without the memo that
    is three full scans times the number of periods, which is what exhausted the
    project's Firestore quota the first time this ran.
    """

    #: Per-request memo, on Flask's ``g`` so it cannot outlive the request.
    _CACHE_KEY = '_au_timetable_periods'

    @staticmethod
    def all_periods() -> List[dict]:
        """Every period row in the department, read once per request."""
        from flask import g, has_request_context

        if not has_request_context():
            return list(store.documents(PERIODS))
        cached = getattr(g, TimetableModel._CACHE_KEY, None)
        if cached is None:
            cached = list(store.documents(PERIODS))
            setattr(g, TimetableModel._CACHE_KEY, cached)
        return cached

    @staticmethod
    def slots() -> dict:
        """Period number to its clock times."""
        return {int(row.get('period')): TimetableSlot(
            period=int(row.get('period')),
            start=row.get('start'),
            end=row.get('end'),
        ) for row in store.documents(SLOTS) if row.get('period')}

    @staticmethod
    def slots_by_section(section: str) -> dict:
        """The clock times *this* section actually uses, falling back to the
        department template.

        Read per section rather than globally because the source disagrees with
        itself: period 2 starts at 10:20 in most sections and 10:30 in the hub
        and interview sections. A leave letter that says "10:30" for a class that
        meets at 10:20 is a document a colleague cannot act on.
        """
        by_period = {}
        for row in TimetableModel.all_periods():
            if row.get('section') != section:
                continue
            period = int(row.get('period') or 0)
            if period and row.get('start'):
                by_period[period] = TimetableSlot(period, row.get('start'),
                                                  row.get('end'))
        return by_period

    @staticmethod
    def periods_for_faculty(faculty_id: int) -> List[dict]:
        """Every period this person is recorded as teaching.

        Only rows with an owner are returned by the query, so the caller does not
        have to re-check. Until the department allocates, this is empty for most
        people, which is exactly why the applicant confirms their own periods
        rather than the system guessing.
        """
        return list(store.documents(PERIODS, faculty_id=faculty_id))

    @staticmethod
    def allocated_by_slot() -> dict:
        """Every allocated period, keyed by ``(section, day, period)``.

        Used by the availability search to know who is genuinely busy. Derived
        from the memoised rows, so the whole page costs one read of the timetable
        rather than one per affected period.
        """
        index = {}
        for row in TimetableModel.all_periods():
            owner = row.get('faculty_id')
            if owner is None:
                continue
            key = (row.get('section'), row.get('day'), int(row.get('period') or 0))
            index.setdefault(key, set()).add(owner)
        return index

    @staticmethod
    def allocation_coverage() -> dict:
        """How much of the timetable has an owner, as a fraction.

        The availability search is only as trustworthy as this number, so it is
        measured rather than assumed and surfaced on the pages that depend on it.
        """
        total = 0
        owned = 0
        for row in TimetableModel.all_periods():
            total += 1
            if row.get('faculty_id') is not None:
                owned += 1
        return {
            'total': total,
            'allocated': owned,
            'missing': total - owned,
            'coverage': (owned / total) if total else 0.0,
        }

    @staticmethod
    def sections_for_faculty(faculty_id: int) -> List[str]:
        """Sections this person is listed to teach a course in.

        Read from ``section_courses`` rather than from owned periods, because the
        published timetable states the teaching *team* per course even where it
        does not state the slots. That is enough to offer someone their own
        sections to pick from.
        """
        sections = {row.get('section') for row in _courses_for_faculty(faculty_id)}
        return sorted(s for s in sections if s)

    @staticmethod
    def courses_for_faculty(faculty_id: int) -> List[dict]:
        return _courses_for_faculty(faculty_id)


def _courses_for_faculty(faculty_id: int) -> List[dict]:
    """The section_courses rows naming this person.

    ``eligible_faculty_ids`` is an array, and Firestore has no way to filter on an
    array field's contents while also returning the document -- the filter that
    works for a membership test is not usable here. The set of sections this
    person teaches is small, so the collection is read once and narrowed in
    Python. Same trade as ``allocated_by_slot``.
    """
    rows = []
    for row in _all_courses():
        if faculty_id in (row.get('eligible_faculty_ids') or []):
            rows.append(row)
    rows.sort(key=lambda r: (r.get('section') or '', r.get('course_code') or ''))
    return rows


def _all_courses() -> List[dict]:
    """Every section_courses row, memoised for the life of the request.

    Read once rather than once per lookup: confirming seven affected periods asks
    for each period's teaching pool, and without this that is seven full reads of
    the collection for one form submission.
    """
    from flask import g, has_request_context

    if not has_request_context():
        return list(store.documents(COURSES))
    cached = getattr(g, '_au_section_courses', None)
    if cached is None:
        cached = list(store.documents(COURSES))
        g._au_section_courses = cached
    return cached


def _eligible_for(course_code: str, section: str) -> List[int]:
    """The teaching pool for a course in a section, as user ids."""
    for row in _all_courses():
        if row.get('course_code') == course_code and row.get('section') == section:
            return [int(i) for i in (row.get('eligible_faculty_ids') or [])]
    return []


class LeaveSummary:
    """Counts over a leave's adjustment rows, attached for display.

    One pass over the rows rather than a count query per state: Firestore has no
    conditional aggregation, and a leave page showing four counts would otherwise
    be four round trips to answer one question.
    """

    @staticmethod
    def attach(leave: FacultyLeave, adjustments: List[ClassAdjustment]
               ) -> FacultyLeave:
        counts = {'ACCEPTED': 0, 'PENDING': 0, 'UNCOVERED': 0,
                  'REJECTED': 0, 'CANCELLED': 0}
        for adjustment in adjustments:
            counts[adjustment.status.value] = \
                counts.get(adjustment.status.value, 0) + 1
        leave.affected_count = len(adjustments)
        leave.accepted_count = counts.get('ACCEPTED', 0)
        leave.uncovered_count = counts.get('UNCOVERED', 0)
        leave.pending_count = counts.get('PENDING', 0)
        leave.adjustment_status = (
            'COMPLETE' if leave.adjustments_resolved
            else 'PARTIAL' if leave.accepted_count else 'PENDING'
        )
        return leave


def _coerce_leave_type(value) -> LeaveType:
    try:
        return LeaveType((value or '').upper())
    except ValueError:
        return LeaveType.OTHER


def _coerce_leave_status(value) -> LeaveStatus:
    try:
        return LeaveStatus((value or '').upper())
    except ValueError:
        return LeaveStatus.DRAFT


def _coerce_adjustment_status(value) -> AdjustmentStatus:
    try:
        return AdjustmentStatus((value or '').upper())
    except ValueError:
        return AdjustmentStatus.PENDING
