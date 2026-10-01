from app.models import User, UserRole, is_roll_id
from app.models.database import db
from datetime import datetime
from typing import Optional, List


def derive_roll_number(email: str, name: str = None) -> Optional[str]:
    """Resolve a student's roll number.

    University mailboxes are provisioned as <rollno>@adityauniversity.in in
    lower case (26b21cs058@...), and the Outlook display name is the same value,
    so either source yields it. The mailbox wins because it stays stable if a
    student renames themselves in Outlook.
    """
    local_part = (email or '').split('@')[0].strip().upper()
    if is_roll_id(local_part):
        return local_part
    candidate = (name or '').strip().upper()
    if is_roll_id(candidate):
        return candidate
    return None


class UserModel:
    @staticmethod
    def find_by_email(email: str) -> Optional[User]:
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM users WHERE email = %s AND is_active = TRUE",
                (email.lower(),)
            )
            row = cursor.fetchone()
            return UserModel._row_to_user(row) if row else None

    @staticmethod
    def find_by_microsoft_id(microsoft_id: str) -> Optional[User]:
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM users WHERE microsoft_id = %s AND is_active = TRUE",
                (microsoft_id,)
            )
            row = cursor.fetchone()
            return UserModel._row_to_user(row) if row else None

    @staticmethod
    def find_by_id(user_id: int) -> Optional[User]:
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM users WHERE id = %s AND is_active = TRUE",
                (user_id,)
            )
            row = cursor.fetchone()
            return UserModel._row_to_user(row) if row else None

    @staticmethod
    def find_by_roll_number(roll_number: str) -> Optional[User]:
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM users WHERE roll_number = %s AND is_active = TRUE",
                (roll_number.upper(),)
            )
            row = cursor.fetchone()
            return UserModel._row_to_user(row) if row else None

    @staticmethod
    def get_lecturers() -> List[User]:
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM users WHERE role = 'LECTURER' AND is_active = TRUE ORDER BY name"
            )
            rows = cursor.fetchall()
            return [UserModel._row_to_user(row) for row in rows]

    @staticmethod
    def get_students() -> List[User]:
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM users WHERE role = 'STUDENT' AND is_active = TRUE ORDER BY roll_number"
            )
            rows = cursor.fetchall()
            return [UserModel._row_to_user(row) for row in rows]

    @staticmethod
    def get_hods() -> List[User]:
        with db.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM users WHERE role = 'HOD' AND is_active = TRUE ORDER BY name"
            )
            rows = cursor.fetchall()
            return [UserModel._row_to_user(row) for row in rows]

    @staticmethod
    def create_or_update_from_microsoft(microsoft_id: str, email: str, name: str) -> User:
        """Provision or refresh a user from a verified Microsoft identity."""
        roll_number = derive_roll_number(email, name)

        existing = UserModel.find_by_microsoft_id(microsoft_id)
        if existing:
            existing.email = email.lower()
            existing.name = name
            if roll_number:
                existing.roll_number = roll_number
            return UserModel.update(existing)

        existing = UserModel.find_by_email(email)
        if existing:
            existing.microsoft_id = microsoft_id
            existing.name = name
            if roll_number:
                existing.roll_number = roll_number
            return UserModel.update(existing)

        return UserModel.create(microsoft_id, email, name, roll_number=roll_number)

    @staticmethod
    def create(microsoft_id: str, email: str, name: str,
               role: UserRole = UserRole.STUDENT, roll_number: str = None,
               phone: str = None, department: str = 'CSE') -> User:
        with db.get_cursor() as cursor:
            cursor.execute(
                """INSERT INTO users
                   (microsoft_id, email, name, role, roll_number, phone,
                    department, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING *""",
                (microsoft_id, email.lower(), name, role.value,
                 roll_number or derive_roll_number(email, name), phone, department,
                 datetime.now(), datetime.now())
            )
            return UserModel._row_to_user(cursor.fetchone())

    @staticmethod
    def update(user: User) -> User:
        with db.get_cursor() as cursor:
            cursor.execute(
                """UPDATE users SET microsoft_id = %s, email = %s, name = %s,
                   role = %s, roll_number = %s, phone = %s, department = %s, updated_at = %s
                   WHERE id = %s
                   RETURNING *""",
                (user.microsoft_id, user.email, user.name, user.role.value,
                 user.roll_number, user.phone, user.department,
                 datetime.now(), user.id)
            )
            # RETURNING rather than a follow-up find_by_id(): the update is not
            # committed until get_cursor() exits, so a second read in another
            # transaction would return the pre-update row. This path runs on
            # every single sign-in.
            row = cursor.fetchone()
            return UserModel._row_to_user(row) if row else None

    @staticmethod
    def set_role(user_id: int, role: UserRole) -> User:
        with db.get_cursor() as cursor:
            cursor.execute(
                "UPDATE users SET role = %s, updated_at = %s WHERE id = %s",
                (role.value, datetime.now(), user_id)
            )
            return UserModel.find_by_id(user_id)

    @staticmethod
    def _row_to_user(row) -> User:
        return User(
            id=row['id'],
            microsoft_id=row['microsoft_id'],
            email=row['email'],
            name=row['name'],
            roll_number=row['roll_number'],
            phone=row['phone'],
            role=UserRole(row['role']),
            department=row['department'],
            is_active=row['is_active'],
            created_at=row['created_at'],
            updated_at=row['updated_at']
        )