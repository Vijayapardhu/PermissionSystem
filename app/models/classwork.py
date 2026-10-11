"""Classwork and the objections it raises against an approved permission.

The problem this solves is narrow and concrete. A student asks for a day off for
an event; the reviewing proctor cannot see the departmental timetable as the
student experiences it, approves it; and it turns out the section had a project
review that morning. Nobody was at fault, the information simply was not in front
of the person deciding.

So a lecturer records the classwork (what, which section, when, how much it
matters), and two things follow:

* the reviewer sees it while the request is still open, and can weigh it against
  the event rather than guessing; and
* classwork created *after* a permission was already granted raises an
  objection, which suspends the grant and sends it back to the same reviewer.

The suspended state is the whole point of the second half. An approved permission
is a letter; the student may have already shown it to a gatekeeper. Reopening it
quietly would leave a printed grant that no longer means anything. So the grant
is suspended, the student is told, and the reviewer decides.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import List, Optional

from app.models.firestore import store

CLASSWORK = 'classwork'
OBJECTIONS = 'classwork_objections'


class ClassworkImportance(Enum):
    """How much a classwork should count against an activity permission.

    Five levels rather than a free-text note, because the reviewer's decision
    turns on this field and a note cannot be compared across requests. The names
    are the ones a lecturer would use; the ordering is what the interface sorts
    on, so "worse than this" is always visible.

    `NOTIFICATION` exists as a deliberate floor. Some sessions -- a doubt class,
    a worksheet hand-in -- are not something a student should lose an event over,
    and without a level below MINOR a lecturer has to either overstate the
    importance to get the panel to notice, or leave the classwork unrecorded. A
    recorded "this did not matter" is better than silence, so it is a first-class
    answer rather than an absence of one.
    """

    NOTIFICATION = 'NOTIFICATION'
    MINOR = 'MINOR'
    MODERATE = 'MODERATE'
    MAJOR = 'MAJOR'
    CRITICAL = 'CRITICAL'


#: Shown on the form, least serious first, because that is the order a lecturer
#: reaches for them in. `(value, label, help_text)` -- the help text is what
#: stops "important" meaning something different in every department.
CLASSWORK_IMPORTANCE_CHOICES = (
    (ClassworkImportance.NOTIFICATION, 'Notification',
     'Doubt session, worksheet, reading. Not a reason to miss an event.'),
    (ClassworkImportance.MINOR, 'Minor',
     'Slip test or quiz. Easy to catch up on.'),
    (ClassworkImportance.MODERATE, 'Moderate',
     'Assignment submission, viva practice, lab record. Missing it costs work.'),
    (ClassworkImportance.MAJOR, 'Major',
     'Project review, internal examination. Missing it cannot be recovered.'),
    (ClassworkImportance.CRITICAL, 'Critical',
     'Practical or semester examination. The session itself is the assessment.'),
)

#: At and above this, the classwork is a reason to object to a granted
#: permission. Below it, creating classwork still informs the panel on a request
#: that is still open, but it does not reopen a permission that was granted --
#: suspending a letter over a worksheet would train people to ignore the queue.
OBJECTION_THRESHOLD = ClassworkImportance.MODERATE

#: Importance above this blocks. `NOTIFICATION` and `MINOR` do not, which is what
#: the panel shows when it recommends a student attend anyway.
BLOCKING_LEVELS = frozenset({
    ClassworkImportance.MAJOR,
    ClassworkImportance.CRITICAL,
})


class ObjectionStatus(Enum):
    """Where a raised objection has reached.

    `ACCEPTED` means the reviewer agreed the objection was worth raising and put
    the permission back. That reads backwards at first glance -- accepting an
    objection restores the grant -- but it is accepting *the objection*, not the
    permission: they accept that the classwork genuinely clashed with what was
    granted, and the original decision is reinstated rather than re-litigated.

    `REJECTED` is the reviewer overruling the lecturer who raised it, and the
    permission ends.

    `WITHDRAWN` is nobody's decision. It is set when the classwork itself
    changed or was deleted, so the claim "this work clashed with that grant"
    stopped being true before anybody answered it. The permission is put back
    at the same moment, because a permission may not stay suspended on the
    strength of a record that no longer exists.
    """

    PENDING = 'PENDING'
    ACCEPTED = 'ACCEPTED'
    REJECTED = 'REJECTED'
    WITHDRAWN = 'WITHDRAWN'


@dataclass
class Classwork:
    """One piece of assessed work a lecturer has scheduled for a section."""

    id: int
    faculty_id: int
    class_id: Optional[int]
    title: str
    date: date
    importance: ClassworkImportance
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    # The timetable slot the work sits in, when the lecturer picked one. Stored
    # alongside the resolved times because the slot is what the panel reads --
    # "period 4" is the shared vocabulary of a department, "10:20 to 11:10" is
    # not, and the two differ between sections.
    period: Optional[int] = None
    # full_day / morning / afternoon, when the lecturer chose the session by hand
    # rather than from the timetable. Mutually exclusive with `period` in practice
    # but not enforced by the type, because a row written before periods existed
    # carries neither and must still read.
    period_label: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Filled in by the query helpers, never stored.
    class_name: Optional[str] = None
    section_code: Optional[str] = None
    faculty_name: Optional[str] = None
    objection_count: int = 0
    # Which of a request's members this piece of classwork actually catches.
    #
    # Set by `find_for_students`, and the reason the field exists at all: on a
    # group request of four students it is normal for one section to have the
    # project review and the other three to be free that day. Showing the entry
    # without saying which member it lands on would have the panel read "there is
    # a project review" and conclude all four are affected, which is the exact
    # error this whole module exists to prevent.
    caught_member_ids: List[int] = field(default_factory=list)

    @property
    def is_full_day(self) -> bool:
        return self.period_label == 'full_day'

    @property
    def session_label(self) -> str:
        """How the times are shown: a slot, a named session, or the clock."""
        if self.period:
            return f'Period {self.period}'
        if self.period_label:
            return {'full_day': 'Full day', 'morning': 'Morning',
                    'afternoon': 'Afternoon'}.get(self.period_label, 'Session')
        if self.start_time and self.end_time:
            return f'{self.start_time:%H:%M}–{self.end_time:%H:%M}'
        return 'All day'

    @property
    def importance_label(self) -> str:
        for value, label, _ in CLASSWORK_IMPORTANCE_CHOICES:
            if value == self.importance:
                return label
        return self.importance.value.title()

    @property
    def importance_help(self) -> str:
        for value, _, help_text in CLASSWORK_IMPORTANCE_CHOICES:
            if value == self.importance:
                return help_text
        return ''

    @property
    def is_blocking(self) -> bool:
        return self.importance in BLOCKING_LEVELS

    @property
    def raises_objections(self) -> bool:
        """Whether creating this can suspend a permission already granted."""
        order = list(ClassworkImportance)
        return order.index(self.importance) >= order.index(OBJECTION_THRESHOLD)

    @property
    def severity_class(self) -> str:
        """Bootstrap badge colour, so the panel reads the queue by eye."""
        return {
            ClassworkImportance.NOTIFICATION: 'bg-secondary',
            ClassworkImportance.MINOR: 'bg-info text-dark',
            ClassworkImportance.MODERATE: 'bg-warning text-dark',
            ClassworkImportance.MAJOR: 'bg-danger',
            ClassworkImportance.CRITICAL: 'bg-danger',
        }.get(self.importance, 'bg-secondary')


@dataclass
class ClassworkObjection:
    """One lecturer's challenge to a permission that was already granted.

    Kept as its own record rather than a flag on the permission, because there
    can be several -- two lecturers both scheduling work into the same granted
    permission -- and each has its own author, its own reason and its own
    decision. Collapsing them into one field would lose the second objection the
    moment the first was answered.
    """

    id: int
    request_id: int
    classwork_id: int
    raised_by: int
    reason: Optional[str]
    status: ObjectionStatus = ObjectionStatus.PENDING
    responded_by: Optional[int] = None
    responded_at: Optional[datetime] = None
    response_remarks: Optional[str] = None
    created_at: Optional[datetime] = None
    # Copy of the permission's reviewer at the moment the objection was raised,
    # so the queue is a query rather than a join. Stored rather than derived
    # because the permission can be reassigned afterwards, and an objection
    # raised against one reviewer should still surface with them.
    assigned_faculty_id: Optional[int] = None
    # Which members of a group permission this classwork actually caught. On a
    # solo request that is every member, which is still worth recording: it is
    # what lets the reviewer say "this is about the roll in CSE-A" rather than
    # leaving the reader to assume four students were all affected.
    affected_member_ids: List[int] = field(default_factory=list)

    # Filled in by the query helpers.
    classwork: Optional[Classwork] = None
    raised_by_name: Optional[str] = None
    responded_by_name: Optional[str] = None

    @property
    def is_pending(self) -> bool:
        return self.status == ObjectionStatus.PENDING

    @property
    def status_label(self) -> str:
        return {
            ObjectionStatus.PENDING: 'Awaiting reviewer',
            ObjectionStatus.ACCEPTED: 'Upheld — permission restored',
            ObjectionStatus.REJECTED: 'Overruled — permission refused',
        }[self.status]


def _to_time(value):
    """Read a stored clock time back, tolerating the shapes the store returns."""
    if value is None or isinstance(value, time):
        return value
    if isinstance(value, str):
        for pattern in ('%H:%M:%S.%f', '%H:%M:%S', '%H:%M'):
            try:
                return datetime.strptime(value, pattern).time()
            except ValueError:
                continue
    return None


def _overlaps(cw: Classwork, start_date, end_date,
              start_time=None, end_time=None) -> bool:
    """Does this classwork sit inside the window a permission covers?

    Date first, then time. A full-day piece of classwork overlaps anything on
    that date; otherwise the two intervals have to genuinely intersect. Touching
    endpoints do not count as overlapping: a classwork starting exactly when the
    permission ends is not a clash, and treating it as one would let a lecturer
    suspend a permission for work the student can still reach.
    """
    if cw.date is None or start_date is None or end_date is None:
        return False
    if cw.date < start_date or cw.date > end_date:
        return False
    if cw.is_full_day or not (cw.start_time and cw.end_time):
        # No resolved times means the whole day, or an unbounded session. Either
        # way it overlaps whatever the permission covers on that date.
        return True
    if not (start_time and end_time):
        return True
    return cw.start_time < end_time and start_time < cw.end_time


def _sort_key(value) -> str:
    return value.isoformat() if hasattr(value, 'isoformat') else str(value or '')


class ClassworkModel:
    @staticmethod
    def create(*, faculty_id: int, class_id: int, title: str, date_: date,
               importance: ClassworkImportance, start_time: time = None,
               end_time: time = None, period: int = None,
               period_label: str = None, notes: str = None) -> Classwork:
        row = store.insert(CLASSWORK, {
            'faculty_id': faculty_id,
            'class_id': class_id,
            'title': title,
            'date': date_,
            'importance': importance.value,
            'start_time': start_time,
            'end_time': end_time,
            'period': period,
            'period_label': period_label,
            'notes': notes,
        })
        return ClassworkModel._to_classwork(row)

    @staticmethod
    def find_by_id(classwork_id: int) -> Optional[Classwork]:
        row = store.get(CLASSWORK, classwork_id)
        return ClassworkModel._to_classwork(row) if row else None

    @staticmethod
    def update(classwork_id: int, **values) -> bool:
        """Merge changed fields into one entry.

        A merge rather than a replace, so a field the caller does not pass
        keeps its stored value. `updated_at` is written by the store's `touch`,
        which is what makes the list's "last changed" column honest -- an edit
        that produced no field change still counts as an edit.
        """
        return store.update(CLASSWORK, classwork_id, values)

    @staticmethod
    def delete(classwork_id: int) -> bool:
        return store.delete(CLASSWORK, classwork_id)

    @staticmethod
    def find_for_faculty(faculty_id: int, class_id: int = None,
                         from_date: date = None, to_date: date = None,
                         limit: int = 100) -> List[Classwork]:
        """One lecturer's own classwork, soonest first.

        Read through the lecturer's rows rather than the whole collection: the
        same store-wide scan the rest of the app makes, but narrowed on a field
        the query can filter without an index.
        """
        rows = list(store.documents(CLASSWORK, faculty_id=faculty_id))
        entries = [ClassworkModel._to_classwork(row) for row in rows]

        if class_id is not None:
            entries = [c for c in entries if c.class_id == class_id]
        if from_date is not None:
            entries = [c for c in entries
                       if c.date is not None and c.date >= from_date]
        if to_date is not None:
            entries = [c for c in entries
                       if c.date is not None and c.date <= to_date]

        entries.sort(key=lambda c: (c.date or date.max, c.start_time or time.min))
        return entries[:limit]

    @staticmethod
    def find_for_students(student_ids, start_date: date, end_date: date,
                          limit: int = 50) -> List[Classwork]:
        """Classwork on these dates that lands on one of these students.

        This is the query behind the reviewer's panel. It cannot ask the store
        "classwork whose class contains any of these students" -- that is a join
        across two collections -- so it walks the classwork on the dates in
        question and asks the roster, which is a collection small enough to hold
        in memory per section.

        One entry per piece of classwork, tagged with the members it catches, so
        the panel can say *which* of the four students on a group request it
        applies to rather than implying it applies to all of them.
        """
        from app.models.classes import MemberModel

        if not student_ids or start_date is None or end_date is None:
            return []

        wanted = {int(s) for s in student_ids}

        # A roster row identifies its student two ways: the linked account id,
        # or the roll number for a row that has not been linked yet. Both answer
        # the same question -- is this one of the request's members -- but they
        # compare against different keys, so they cannot be unioned into a single
        # set. Resolving rolls to member ids up front keeps caught_member_ids a
        # list of ids, which is what every caller and template expects.
        roll_to_member = {}
        for member_id in wanted:
            row = store.get('users', member_id)
            roll = str((row or {}).get('roll_number') or '').strip().upper()
            if roll:
                roll_to_member[roll] = member_id

        class_ids = set()
        for row in store.documents(CLASSWORK):
            when = row.get('date')
            if when is None or when < start_date or when > end_date:
                continue
            class_ids.add(row.get('class_id'))

        matches = []
        for class_id in class_ids:
            if class_id is None:
                continue
            roster = MemberModel.find_by_class(class_id)
            if not roster:
                continue

            caught = {m.student_id for m in roster
                      if m.student_id and m.student_id in wanted}
            for member in roster:
                roll = str(member.roll_number or '').strip().upper()
                member_id = roll_to_member.get(roll) if roll else None
                if member_id is not None:
                    caught.add(member_id)
            if not caught:
                continue

            for row in store.documents(CLASSWORK, class_id=class_id):
                entry = ClassworkModel._to_classwork(row)
                if _overlaps(entry, start_date, end_date):
                    entry.caught_member_ids = sorted(caught)
                    matches.append(entry)

        matches.sort(key=lambda c: (c.date or date.max,
                                    c.start_time or time.min))
        return matches[:limit]

    @staticmethod
    def _to_classwork(row) -> Classwork:
        try:
            importance = ClassworkImportance(row.get('importance'))
        except ValueError:
            # A level added to the enum after this row was written, or a bad
            # hand-edit. Falling back to the least alarming level keeps the row
            # visible instead of raising out of a list comprehension and taking
            # a whole page down over one record.
            importance = ClassworkImportance.NOTIFICATION
        return Classwork(
            id=int(row['id']),
            faculty_id=row.get('faculty_id'),
            class_id=row.get('class_id'),
            title=row.get('title') or '',
            date=row.get('date'),
            importance=importance,
            start_time=_to_time(row.get('start_time')),
            end_time=_to_time(row.get('end_time')),
            period=row.get('period'),
            period_label=row.get('period_label'),
            notes=row.get('notes'),
            created_at=row.get('created_at'),
            updated_at=row.get('updated_at') or row.get('created_at'),
        )


def attach_classwork_context(entries: List[Classwork]) -> List[Classwork]:
    """Fill in the class name, section and author for a list of classwork."""
    from app.models.classes import ClassModel
    from app.models.user import UserModel

    class_ids = {c.class_id for c in entries if c.class_id}
    faculty_ids = {c.faculty_id for c in entries if c.faculty_id}

    classes = {}
    for class_id in class_ids:
        group = ClassModel.find_by_id(class_id)
        if group:
            classes[class_id] = group

    names = {}
    for faculty_id in faculty_ids:
        user = UserModel.find_by_id(faculty_id)
        if user:
            names[faculty_id] = user.name

    for entry in entries:
        group = classes.get(entry.class_id)
        if group:
            entry.class_name = group.name
            entry.section_code = group.section_code
        entry.faculty_name = names.get(entry.faculty_id)
    return entries


class ObjectionModel:
    @staticmethod
    def create(*, request_id: int, classwork_id: int, raised_by: int,
               reason: str = None) -> ClassworkObjection:
        row = store.insert(OBJECTIONS, {
            'request_id': request_id,
            'classwork_id': classwork_id,
            'raised_by': raised_by,
            'reason': reason,
            'status': ObjectionStatus.PENDING.value,
        })
        return ObjectionModel._to_objection(row)

    @staticmethod
    def find_by_id(objection_id: int) -> Optional[ClassworkObjection]:
        row = store.get(OBJECTIONS, objection_id)
        return ObjectionModel._to_objection(row) if row else None

    @staticmethod
    def find_by_request(request_id: int) -> List[ClassworkObjection]:
        rows = store.documents(OBJECTIONS, request_id=request_id)
        entries = [ObjectionModel._to_objection(row) for row in rows]
        entries.sort(key=lambda o: _sort_key(o.created_at))
        return _attach_context(entries)

    @staticmethod
    def find_pending_for_faculty(faculty_id: int) -> List[ClassworkObjection]:
        """Open objections addressed to this reviewer, oldest first.

        `assigned_faculty_id` on the objection rather than a join against the
        permission: the permission's reviewer is copied here when the objection
        is raised, so the queue does not have to re-derive who should have seen
        it from a request that may since have been reassigned.
        """
        rows = store.documents(OBJECTIONS,
                               assigned_faculty_id=faculty_id,
                               status=ObjectionStatus.PENDING.value)
        entries = [ObjectionModel._to_objection(row) for row in rows]
        entries.sort(key=lambda o: _sort_key(o.created_at))
        return _attach_context(entries)

    @staticmethod
    def find_pending() -> List[ClassworkObjection]:
        """Every open objection, for the HOD's overview."""
        rows = store.documents(OBJECTIONS,
                               status=ObjectionStatus.PENDING.value)
        entries = [ObjectionModel._to_objection(row) for row in rows]
        entries.sort(key=lambda o: _sort_key(o.created_at))
        return _attach_context(entries)

    @staticmethod
    def _store_reviewer(objection_id: int, faculty_id: int) -> bool:
        """Copy the permission's reviewer onto the objection.

        Written after creation rather than passed in, because the objection is
        raised by the lecturer who scheduled the classwork and they have no
        business naming the reviewer -- the service reads it off the permission
        and passes it down.
        """
        return store.update(OBJECTIONS, objection_id,
                            {'assigned_faculty_id': faculty_id})

    @staticmethod
    def respond(objection_id: int, reviewer_id: int,
                status: ObjectionStatus, remarks: str = None) -> bool:
        # `store.update` has no timestamps= argument -- it stamps `updated_at`
        # and nothing else -- so the decision time is written as a real field.
        # A response with no time on it cannot be aged or audited, and this is
        # the one row whose whole purpose is to be dated.
        from app.models.firestore import utc_now

        return store.update(OBJECTIONS, objection_id, {
            'status': status.value,
            'responded_by': reviewer_id,
            'response_remarks': remarks,
            'responded_at': utc_now(),
        })

    @staticmethod
    def _to_objection(row) -> ClassworkObjection:
        try:
            status = ObjectionStatus(row.get('status'))
        except ValueError:
            status = ObjectionStatus.PENDING
        return ClassworkObjection(
            id=int(row['id']),
            request_id=row.get('request_id'),
            classwork_id=row.get('classwork_id'),
            raised_by=row.get('raised_by'),
            reason=row.get('reason'),
            status=status,
            responded_by=row.get('responded_by'),
            responded_at=row.get('responded_at'),
            response_remarks=row.get('response_remarks'),
            created_at=row.get('created_at'),
            assigned_faculty_id=row.get('assigned_faculty_id'),
            affected_member_ids=[
                int(m) for m in (row.get('affected_member_ids') or [])
                if isinstance(m, (int, float)) or str(m).isdigit()
            ],
        )


def _attach_context(entries: List[ClassworkObjection]) -> List[ClassworkObjection]:
    """Fill in the classwork each objection points at, and who raised it."""
    from app.models.user import UserModel

    work = {}
    for entry in entries:
        if entry.classwork_id and entry.classwork_id not in work:
            found = ClassworkModel.find_by_id(entry.classwork_id)
            if found:
                work[entry.classwork_id] = found

    people = set()
    for entry in entries:
        if entry.raised_by:
            people.add(entry.raised_by)
        if entry.responded_by:
            people.add(entry.responded_by)

    names = {}
    for person_id in people:
        user = UserModel.find_by_id(person_id)
        if user:
            names[person_id] = user.name

    for entry in entries:
        entry.classwork = work.get(entry.classwork_id)
        entry.raised_by_name = names.get(entry.raised_by)
        entry.responded_by_name = names.get(entry.responded_by)
    return entries