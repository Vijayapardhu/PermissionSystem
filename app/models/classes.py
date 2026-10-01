"""Class groups, rosters and attendance.

A lecturer creates a class, bulk-loads the roster from a spreadsheet of roll
numbers, then views that class's permissions for any date and marks attendance.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

from app.models.database import db


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


class ClassModel:
    @staticmethod
    def create(name: str, faculty_id: int, section_code: str = None,
               academic_year: str = None) -> ClassGroup:
        with db.get_cursor() as cursor:
            cursor.execute(
                """INSERT INTO class_groups (name, section_code, academic_year, faculty_id, created_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (name.strip(), (section_code or None), (academic_year or None),
                 faculty_id, datetime.now()),
            )
            return ClassModel.find_by_id(cursor.lastrowid)

    @staticmethod
    def find_by_id(class_id: int) -> Optional[ClassGroup]:
        with db.get_cursor() as cursor:
            cursor.execute(
                """SELECT c.*,
                          (SELECT COUNT(*) FROM class_members m WHERE m.class_id = c.id) AS member_count,
                          (SELECT COUNT(*) FROM class_members m
                            WHERE m.class_id = c.id AND m.student_id IS NOT NULL) AS linked_count
                   FROM class_groups c WHERE c.id = %s""",
                (class_id,),
            )
            row = cursor.fetchone()
            return _row_to_class(row) if row else None

    @staticmethod
    def find_for_faculty(faculty_id: int) -> List[ClassGroup]:
        with db.get_cursor() as cursor:
            cursor.execute(
                """SELECT c.*,
                          (SELECT COUNT(*) FROM class_members m WHERE m.class_id = c.id) AS member_count,
                          (SELECT COUNT(*) FROM class_members m
                            WHERE m.class_id = c.id AND m.student_id IS NOT NULL) AS linked_count
                   FROM class_groups c
                   WHERE c.faculty_id = %s
                   ORDER BY c.created_at DESC""",
                (faculty_id,),
            )
            return [_row_to_class(row) for row in cursor.fetchall()]

    @staticmethod
    def delete(class_id: int) -> bool:
        with db.get_cursor() as cursor:
            cursor.execute('DELETE FROM class_groups WHERE id = %s', (class_id,))
            return cursor.rowcount > 0


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

            with db.get_cursor() as cursor:
                cursor.execute(
                    'SELECT id, roll_number FROM users WHERE roll_number = %s',
                    (roll,),
                )
                student = cursor.fetchone()
                student_id = student['id'] if student else None

                cursor.execute(
                    'SELECT id FROM class_members WHERE class_id = %s AND roll_number = %s',
                    (class_id, roll),
                )
                existing = cursor.fetchone()

                if existing:
                    cursor.execute(
                        'UPDATE class_members SET student_id = %s WHERE id = %s',
                        (student_id, existing['id']),
                    )
                else:
                    cursor.execute(
                        """INSERT INTO class_members
                           (class_id, student_id, roll_number, enrolled, created_at)
                           VALUES (%s, %s, %s, TRUE, %s)""",
                        (class_id, student_id, roll, datetime.now()),
                    )
                    added += 1

            if student_id:
                linked += 1
            else:
                unresolved.append(roll)

        return {'added': added, 'linked': linked, 'unresolved': unresolved}

    @staticmethod
    def find_by_class(class_id: int) -> List[ClassMember]:
        with db.get_cursor() as cursor:
            cursor.execute(
                """SELECT m.*, u.name AS student_name, u.email AS student_email, u.phone
                   FROM class_members m
                   LEFT JOIN users u ON u.id = m.student_id
                   WHERE m.class_id = %s
                   ORDER BY m.roll_number""",
                (class_id,),
            )
            return [_row_to_member(row) for row in cursor.fetchall()]

    @staticmethod
    def find_by_class_and_rolls(class_id: int, rolls: List[str]):
        if not rolls:
            return {}
        with db.get_cursor() as cursor:
            placeholders = ', '.join(['%s'] * len(rolls))
            cursor.execute(
                f"""SELECT m.*, u.name AS student_name, u.email AS student_email, u.phone
                    FROM class_members m
                    LEFT JOIN users u ON u.id = m.student_id
                    WHERE m.class_id = %s AND m.roll_number IN ({placeholders})""",
                [class_id] + [r.strip().upper() for r in rolls],
            )
            return {row['roll_number']: _row_to_member(row)
                    for row in cursor.fetchall()}

    @staticmethod
    def delete_member(member_id: int) -> bool:
        with db.get_cursor() as cursor:
            cursor.execute('DELETE FROM class_members WHERE id = %s', (member_id,))
            return cursor.rowcount > 0

    @staticmethod
    def relink_unresolved() -> int:
        """Attach student accounts to roster rows saved before first sign-in."""
        with db.get_cursor() as cursor:
            cursor.execute(
                """UPDATE class_members m
                   JOIN users u ON u.roll_number = m.roll_number
                   SET m.student_id = u.id
                   WHERE m.student_id IS NULL"""
            )
            return cursor.rowcount


class AttendanceModel:
    @staticmethod
    def save(class_id: int, records: List[tuple], marked_by: int,
             attendance_date: date) -> int:
        """Upsert one row per (student, date). records is [(student_id, status)]."""
        saved = 0
        with db.get_cursor() as cursor:
            for student_id, status in records:
                cursor.execute(
                    """INSERT INTO attendance_records
                       (class_id, student_id, attendance_date, status, marked_by, marked_at)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE status = VALUES(status),
                                               marked_by = VALUES(marked_by),
                                               marked_at = VALUES(marked_at)""",
                    (class_id, student_id, attendance_date, status, marked_by,
                     datetime.now()),
                )
                saved += 1
        return saved

    @staticmethod
    def find_by_class_date(class_id: int, attendance_date: date) -> dict:
        with db.get_cursor() as cursor:
            cursor.execute(
                """SELECT student_id, status FROM attendance_records
                   WHERE class_id = %s AND attendance_date = %s""",
                (class_id, attendance_date),
            )
            return {row['student_id']: row['status'] for row in cursor.fetchall()}

    @staticmethod
    def summary_by_date(class_id: int, attendance_date: date) -> dict:
        with db.get_cursor() as cursor:
            cursor.execute(
                """SELECT status, COUNT(*) AS n FROM attendance_records
                   WHERE class_id = %s AND attendance_date = %s
                   GROUP BY status""",
                (class_id, attendance_date),
            )
            counts = {'PRESENT': 0, 'ABSENT': 0, 'ON_PERMISSION': 0}
            for row in cursor.fetchall():
                counts[row['status']] = row['n']
            counts['total'] = sum(counts.values())
            return counts


def _row_to_class(row) -> ClassGroup:
    return ClassGroup(
        id=row['id'],
        name=row['name'],
        section_code=row.get('section_code'),
        academic_year=row.get('academic_year'),
        faculty_id=row['faculty_id'],
        created_at=row['created_at'],
        member_count=row.get('member_count') or 0,
        linked_count=row.get('linked_count') or 0,
    )


def _row_to_member(row) -> ClassMember:
    return ClassMember(
        id=row['id'],
        class_id=row['class_id'],
        student_id=row.get('student_id'),
        roll_number=row['roll_number'],
        enrolled=bool(row.get('enrolled', True)),
        created_at=row['created_at'],
        student_name=row.get('student_name'),
        student_email=row.get('student_email'),
        phone=row.get('phone'),
    )