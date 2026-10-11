"""Class groups and rosters.

A lecturer creates a class, bulk-loads the roster from a spreadsheet of roll
numbers, then views that class's permissions for any date.

There is no attendance here, and deliberately so: the department does not
mark it in this system. An earlier version recorded it, which added a screen
to fill in, a column to read, and a second question on the class page -- for
a record nobody entered and nobody read. The permission view below is what
the class page is for.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

from app.models.firestore import store

GROUPS = 'class_groups'
MEMBERS = 'class_members'

# The two things a lecturer keeps lists of. A CLASS is taught: it has a roster,
# a subject and a timetable. A PROCTOR group is mentored: its rolls are the
# students whose permission requests land in that lecturer's queue. Same
# storage, because a proctor list is uploaded and linked exactly like a roster
# -- only the meaning and the pages differ.
KIND_CLASS = 'CLASS'
KIND_PROCTOR = 'PROCTOR'


@dataclass
class ClassGroup:
    id: int
    name: str
    section_code: Optional[str]
    academic_year: Optional[str]
    faculty_id: int
    created_at: datetime
    member_count: int = 0
    linked_count: int = 0
    kind: str = KIND_CLASS

    @property
    def is_proctor_group(self) -> bool:
        """True when this group is a proctor list rather than a taught class."""
        return (self.kind or KIND_CLASS) == KIND_PROCTOR


@dataclass
class ClassMember:
    id: int
    class_id: int
    student_id: Optional[int]
    roll_number: str
    enrolled: bool
    created_at: datetime
    student_name: Optional[str] = None
    student_email: Optional[str] = None
    phone: Optional[str] = None

    @property
    def is_linked(self) -> bool:
        """True once the roster entry maps to a real student account."""
        return self.student_id is not None


def _member_counts() -> dict:
    """Roster sizes per class: (members, linked).

    One read of the whole roster collection, because counting per class would
    mean a query per class and a `where('class_id', 'in', [...])` is not a thing
    in Firestore. A department has tens of classes, not thousands.
    """
    counts = {}
    for row in store.documents(MEMBERS):
        class_id = row.get('class_id')
        total, linked = counts.get(class_id, (0, 0))
        counts[class_id] = (total + 1, linked + (1 if row.get('student_id') else 0))
    return counts


def _student_directory(student_ids) -> dict:
    """Users keyed by id, for the fields the old LEFT JOIN supplied."""
    directory = {}
    for student_id in {sid for sid in student_ids if sid}:
        row = store.get('users', student_id)
        if row:
            directory[student_id] = row
    return directory


class ClassModel:
    @staticmethod
    def create(name: str, faculty_id: int, section_code: str = None,
               academic_year: str = None,
               kind: str = KIND_CLASS) -> ClassGroup:
        row = store.insert(GROUPS, {
            'name': (name or '').strip(),
            'section_code': section_code or None,
            'academic_year': academic_year or None,
            'faculty_id': faculty_id,
            # Rows written before proctor groups existed carry no kind and read
            # back as CLASS, so every existing class keeps meaning what it did.
            'kind': kind or KIND_CLASS,
        })
        # A class that was just created has no members, so member_count and
        # linked_count are zero by definition.
        return _to_class(row, (0, 0))

    @staticmethod
    def find_by_id(class_id: int) -> Optional[ClassGroup]:
        row = store.get(GROUPS, class_id)
        if not row:
            return None
        return _to_class(row, _member_counts().get(row['id'], (0, 0)))

    @staticmethod
    def update_details(class_id: int, name: str, section_code: str = None,
                       academic_year: str = None) -> bool:
        """Change a group's own description.

        `faculty_id` is deliberately not a parameter. Moving a class to another
        lecturer is a transfer, not a rename: it decides whose queue the class's
        permission requests land in, and the roster would have to move with it.
        Doing that quietly from a text field is how a class ends up owned by
        somebody who never agreed to teach it, so a transfer is a separate,
        deliberate action.
        """
        return store.update(GROUPS, class_id, {
            'name': (name or '').strip(),
            'section_code': (section_code or '').strip() or None,
            'academic_year': (academic_year or '').strip() or None,
        })

    @staticmethod
    def find_by_name(name: str, exclude_id: int = None) -> Optional[ClassGroup]:
        """A group with this name, for refusing a duplicate on rename."""
        wanted = (name or '').strip().casefold()
        if not wanted:
            return None
        for row in store.documents(GROUPS):
            if row.get('id') == exclude_id:
                continue
            if (row.get('name') or '').strip().casefold() == wanted:
                return _to_class(row, (0, 0))
        return None

    @staticmethod
    def find_for_faculty(faculty_id: int, kind: str = None) -> List[ClassGroup]:
        """The lecturer's groups, optionally only one kind.

        `kind=None` keeps the old behaviour of returning everything, so callers
        that do not care (the HOD overview) need no change.
        """
        counts = _member_counts()
        rows = store.documents(GROUPS, faculty_id=faculty_id)
        groups = [_to_class(row, counts.get(row['id'], (0, 0))) for row in rows]
        if kind:
            groups = [g for g in groups if (g.kind or KIND_CLASS) == kind]
        groups.sort(key=lambda g: (_sort_key(g.created_at), g.id), reverse=True)
        return groups

    @staticmethod
    def delete(class_id: int) -> bool:
        """Delete a class and everything hanging off it.

        Postgres did this with ON DELETE CASCADE. Firestore has no referential
        integrity, so the cascade is written out: leaving orphaned roster rows
        behind would show them on a class that no longer exists.
        """
        for row in store.documents(MEMBERS, class_id=class_id):
            store.delete(MEMBERS, row['id'])
        return store.delete(GROUPS, class_id)


class MemberModel:
    @staticmethod
    def add_members(class_id: int, roll_numbers: List[str]) -> dict:
        """Insert roster rows, linking to existing students where possible.

        Existing rows are updated rather than duplicated, so a roster can be
        re-uploaded after corrections.
        """
        added, linked, unresolved = 0, 0, []
        seen = set()

        for raw in roll_numbers:
            roll = (raw or '').strip().upper()
            if not roll or roll in seen:
                continue
            seen.add(roll)

            student = _student_by_roll(roll)
            student_id = student['id'] if student else None

            existing = _member_row(class_id, roll)
            if existing:
                store.update(MEMBERS, existing['id'], {'student_id': student_id})
            else:
                store.insert(MEMBERS, {
                    'class_id': class_id,
                    'student_id': student_id,
                    'roll_number': roll,
                    'enrolled': True,
                })
                added += 1

            if student_id:
                linked += 1
            else:
                unresolved.append(roll)

        return {'added': added, 'linked': linked, 'unresolved': unresolved}

    @staticmethod
    def find_by_class(class_id: int) -> List[ClassMember]:
        rows = store.documents(MEMBERS, class_id=class_id)
        members = [_to_member(row) for row in rows]
        members.sort(key=lambda m: (m.roll_number or '', m.id or 0))
        _attach_students(members)
        return members

    @staticmethod
    def find_by_class_and_rolls(class_id: int, rolls: List[str]) -> dict:
        wanted = {(r or '').strip().upper() for r in rolls if r}
        if not wanted:
            return {}
        members = [
            member for member in MemberModel.find_by_class(class_id)
            if member.roll_number in wanted
        ]
        return {member.roll_number: member for member in members}

    @staticmethod
    def delete_member(member_id: int) -> bool:
        return store.delete(MEMBERS, member_id)

    @staticmethod
    def relink_unresolved() -> int:
        """Attach student accounts to roster rows saved before first sign-in."""
        pending = [row for row in store.documents(MEMBERS)
                   if not row.get('student_id')]
        linked = 0
        for row in pending:
            student = _student_by_roll(row.get('roll_number'))
            if not student:
                continue
            if store.update(MEMBERS, row['id'], {'student_id': student['id']},
                            touch=False):
                linked += 1
        return linked


class ProctorModel:
    """Proctor lists: which lecturer reviews which students.

    Kept apart from `ClassModel` on purpose. A class is taught -- it has a
    timetable and a subject. A proctor list is neither: it is the
    college's allocation of mentoring responsibility, and the only thing it
    decides is whose permission requests land in that lecturer's queue.

    They share the `class_groups` collection, which is why reading them needs a
    `kind` filter rather than a different table. Every read that reaches a
    student-facing or teaching-facing page goes through here, so the two can
    never be confused again at the call site.
    """

    @staticmethod
    def groups_for_faculty(faculty_id: int) -> List[ClassGroup]:
        return ClassModel.find_for_faculty(faculty_id, kind=KIND_PROCTOR)

    @staticmethod
    def groups_for_student(student_id: int) -> List[ClassGroup]:
        """Every proctor list this student is on, oldest first.

        Ordered by id on purpose. A student can end up on two lists -- a
        transfer, or a batch re-allocated without the old list being cleaned up
        -- and returning whichever the store happened to yield first made the
        proctor shown on the student's page depend on read order rather than on
        anything true about the student.

        This is a list rather than a single value because the right answer for
        "who reviews this student's requests" is not always the first list; see
        the note on `group_for_student`.
        """
        found = []
        for row in store.documents(MEMBERS, student_id=student_id):
            group = ClassModel.find_by_id(row.get('class_id'))
            if group is not None and group.is_proctor_group:
                found.append(group)
        found.sort(key=lambda g: g.id)
        return found

    @staticmethod
    def group_for_student(student_id: int) -> Optional[ClassGroup]:
        """The proctor list this student is on, or None.

        Keyed on the linked account id rather than the roll number, because the
        roll is what can be mistyped on a spreadsheet while the account id is
        what the rest of the system identifies a student by. A roster row that
        has never been linked falls back to the roll, since that is all it has.

        With more than one list this returns the first, which is NOT necessarily
        the proctor who will review their requests: `find_proctor_for_student`
        breaks that tie by who has the smaller open queue, because that is the
        rule routing actually uses. Anything that tells a student who their
        proctor is must go through the service for that reason -- reporting the
        first list's owner would be showing them one person while their request
        is decided by another.
        """
        groups = ProctorModel.groups_for_student(student_id)
        return groups[0] if groups else None

    @staticmethod
    def proctor_for_student_id(student_id: int) -> Optional[int]:
        """The faculty id of the proctor responsible for this student."""
        group = ProctorModel.group_for_student(student_id)
        return group.faculty_id if group else None

    @staticmethod
    def student_ids_in_group(group_id: int) -> set:
        """Every student on one proctor list.

        Used for two things: naming the students who share a proctor on the
        student's own page, and deciding whether a request in a lecturer's
        queue came from their own proctor list or was routed to them some other
        way.
        """
        return {row.get('student_id')
                for row in store.documents(MEMBERS, class_id=group_id)
                if row.get('student_id')}

    @staticmethod
    def student_ids_for_faculty(faculty_id: int) -> set:
        """Every student this lecturer proctors, across all their lists."""
        proctored = set()
        for group in ProctorModel.groups_for_faculty(faculty_id):
            proctored |= ProctorModel.student_ids_in_group(group.id)
        return proctored

    @staticmethod
    def rolls_in_group(group_id: int) -> List[str]:
        """The roll numbers on one proctor list, sorted."""
        rolls = [str(row.get('roll_number') or '').strip().upper()
                 for row in store.documents(MEMBERS, class_id=group_id)]
        return sorted({r for r in rolls if r})

    @staticmethod
    def add_students(group_id: int, roll_numbers: List[str]) -> dict:
        """Put these students on a proctor list.

        Delegates to `MemberModel.add_members` rather than duplicating the
        upsert, so a proctor list and a class roster stay identical in how they
        store and link a student -- the only thing that differs between them is
        the meaning of the list.
        """
        return MemberModel.add_members(group_id, roll_numbers)

    @staticmethod
    def search_students(query: str, limit: int = 12) -> List[dict]:
        """Students matching a roll number or name, for the add-a-student box.

        Scoped to students in this department by construction: the search walks
        the same roster the proctor already uses rather than the whole directory,
        so a proctor cannot add a student from another department to their list
        by guessing a roll number.

        Deliberately not a general directory search. The faculty search page
        already answers "who is this student", and reusing it here would imply
        that anyone on it can be added to any list -- which for proctoring is
        not a thing the college does.
        """
        from app.models.user import UserModel
        from app.models import UserRole

        needle = (query or '').strip().lower()
        if len(needle) < 2:
            return []

        found, seen = [], set()
        for row in store.documents(MEMBERS):
            roll = str(row.get('roll_number') or '').lower()
            student_id = row.get('student_id')
            if not student_id or student_id in seen:
                continue
            student = UserModel.find_by_id(student_id)
            if student is None or student.role != UserRole.STUDENT:
                continue
            if needle not in roll and needle not in (student.name or '').lower():
                continue
            seen.add(student_id)
            found.append({'id': student_id, 'roll_number': student.roll_number,
                          'name': student.name, 'email': student.email or ''})
            if len(found) >= limit:
                break
        return found


def _member_row(class_id: int, roll_number: str):
    for row in store.documents(MEMBERS, class_id=class_id,
                               roll_number=roll_number):
        return row
    return None


def _student_by_roll(roll_number: str):
    if not roll_number:
        return None
    for row in store.documents('users', roll_number=roll_number):
        return row
    return None


def _attach_students(members: List[ClassMember]) -> None:
    directory = _student_directory(m.student_id for m in members)
    for member in members:
        student = directory.get(member.student_id)
        if not student:
            continue
        member.student_name = student.get('name')
        member.student_email = student.get('email')
        member.phone = student.get('phone')


def _sort_key(value) -> str:
    return value.isoformat() if hasattr(value, 'isoformat') else str(value or '')


def _to_class(row, counts=(0, 0)) -> ClassGroup:
    return ClassGroup(
        id=int(row['id']),
        name=row.get('name') or '',
        section_code=row.get('section_code'),
        academic_year=row.get('academic_year'),
        faculty_id=row.get('faculty_id'),
        created_at=row.get('created_at'),
        member_count=counts[0],
        linked_count=counts[1],
        kind=row.get('kind') or KIND_CLASS,
    )


def _to_member(row) -> ClassMember:
    return ClassMember(
        id=int(row['id']),
        class_id=row.get('class_id'),
        student_id=row.get('student_id'),
        roll_number=row.get('roll_number') or '',
        enrolled=bool(row.get('enrolled', True)),
        created_at=row.get('created_at'),
    )