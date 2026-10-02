"""Class groups, rosters and attendance.

A lecturer creates a class, bulk-loads the roster from a spreadsheet of roll
numbers, then views that class's permissions for any date and marks attendance.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

from app.models.firestore import store

GROUPS = 'class_groups'
MEMBERS = 'class_members'
ATTENDANCE = 'attendance_records'


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


@dataclass
class AttendanceEntry:
    id: Optional[int]
    class_id: int
    student_id: int
    attendance_date: date
    status: str
    marked_by: Optional[int] = None
    marked_at: Optional[datetime] = None


@dataclass
class ClassPermissions:
    """A single student's permissions that overlap a given date."""

    member: ClassMember
    permissions: List = field(default_factory=list)
    attendance_status: Optional[str] = None


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
               academic_year: str = None) -> ClassGroup:
        row = store.insert(GROUPS, {
            'name': (name or '').strip(),
            'section_code': section_code or None,
            'academic_year': academic_year or None,
            'faculty_id': faculty_id,
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
    def find_for_faculty(faculty_id: int) -> List[ClassGroup]:
        counts = _member_counts()
        rows = store.documents(GROUPS, faculty_id=faculty_id)
        groups = [_to_class(row, counts.get(row['id'], (0, 0))) for row in rows]
        groups.sort(key=lambda g: (_sort_key(g.created_at), g.id), reverse=True)
        return groups

    @staticmethod
    def delete(class_id: int) -> bool:
        """Delete a class and everything hanging off it.

        Postgres did this with ON DELETE CASCADE. Firestore has no referential
        integrity, so the cascade is written out: leaving orphaned roster rows and
        attendance behind would show them on a class that no longer exists.
        """
        for row in store.documents(MEMBERS, class_id=class_id):
            store.delete(MEMBERS, row['id'])
        store.delete_where(ATTENDANCE, class_id=class_id)
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


class AttendanceModel:
    @staticmethod
    def _doc_id(class_id, student_id, attendance_date) -> str:
        """The natural key of an attendance mark, as the document id.

        One row per student per class per day is the rule the old UNIQUE constraint
        enforced. Making it the document id means a second save for the same day
        overwrites the first, with no read to decide whether it should.
        """
        return f'{class_id}:{student_id}:{attendance_date}'

    @staticmethod
    def save(class_id: int, records: List[tuple], marked_by: int,
             attendance_date: date) -> int:
        """Upsert one mark per (student, date). records is [(student_id, status)]."""
        saved = 0
        day = attendance_date.isoformat() if hasattr(
            attendance_date, 'isoformat') else str(attendance_date)
        for student_id, status in records:
            store.insert(
                ATTENDANCE,
                {
                    'class_id': class_id,
                    'student_id': student_id,
                    'attendance_date': day,
                    'status': status,
                    'marked_by': marked_by,
                },
                doc_id=AttendanceModel._doc_id(class_id, student_id, day),
                timestamps=('marked_at',),
            )
            saved += 1
        return saved

    @staticmethod
    def find_by_class_date(class_id: int, attendance_date: date) -> dict:
        day = attendance_date.isoformat() if hasattr(
            attendance_date, 'isoformat') else str(attendance_date)
        rows = store.documents(ATTENDANCE, class_id=class_id,
                               attendance_date=day)
        return {row.get('student_id'): row.get('status') for row in rows}

    @staticmethod
    def summary_by_date(class_id: int, attendance_date: date) -> dict:
        counts = {'PRESENT': 0, 'ABSENT': 0, 'ON_PERMISSION': 0}
        # The mapping is student id -> status, so the status is the value.
        for status in AttendanceModel.find_by_class_date(
                class_id, attendance_date).values():
            if status in counts:
                counts[status] += 1
        counts['total'] = sum(counts.values())
        return counts


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