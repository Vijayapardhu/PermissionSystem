from datetime import datetime
from typing import Optional, List

from app.models import User, UserRole, is_roll_id
from app.models.firestore import store

COLLECTION = 'users'


def derive_roll_number(email: str, name: str = None) -> Optional[str]:
    """Resolve a student's roll number.

    University mailboxes are provisioned as <rollno>@adityauniversity.in in lower
    case (26b21cs058@...), and the Outlook display name is the same value, so
    either source yields it. The mailbox wins because it stays stable if a student
    renames themselves in Outlook.
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
    def _find_one(**filters) -> Optional[User]:
        filters['is_active'] = True
        rows = store.documents(COLLECTION, **filters)
        return UserModel._to_user(rows[0]) if rows else None

    @staticmethod
    def find_by_email(email: str) -> Optional[User]:
        return UserModel._find_one(email=(email or '').lower())

    @staticmethod
    def find_by_microsoft_id(microsoft_id: str) -> Optional[User]:
        return UserModel._find_one(microsoft_id=microsoft_id)

    @staticmethod
    def find_by_id(user_id: int) -> Optional[User]:
        return UserModel._find_one(id=user_id)

    @staticmethod
    def find_by_roll_number(roll_number: str) -> Optional[User]:
        return UserModel._find_one(roll_number=(roll_number or '').upper())

    @staticmethod
    def _by_role(role: str, order_by: str) -> List[User]:
        rows = store.documents(COLLECTION, role=role, is_active=True)
        users = [UserModel._to_user(row) for row in rows]
        # Sorting here rather than with order_by: Firestore would need a composite
        # index for a role filter plus an ordering, and a missing index is an
        # error at request time rather than a slower query.
        return sorted(users, key=lambda u: (getattr(u, order_by) or '', u.name))

    @staticmethod
    def get_lecturers() -> List[User]:
        return UserModel._by_role(UserRole.LECTURER.value, 'name')

    @staticmethod
    def get_students() -> List[User]:
        return UserModel._by_role(UserRole.STUDENT.value, 'roll_number')

    @staticmethod
    def get_hods() -> List[User]:
        return UserModel._by_role(UserRole.HOD.value, 'name')

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
        row = store.insert(COLLECTION, {
            'microsoft_id': microsoft_id,
            'email': email.lower(),
            'name': name,
            'role': role.value,
            'roll_number': roll_number or derive_roll_number(email, name),
            'phone': phone,
            'department': department,
            'is_active': True,
        })
        return UserModel._to_user(row)

    @staticmethod
    def update(user: User) -> Optional[User]:
        store.update(COLLECTION, user.id, {
            'microsoft_id': user.microsoft_id,
            'email': user.email,
            'name': user.name,
            'role': user.role.value,
            'roll_number': user.roll_number,
            'phone': user.phone,
            'department': user.department,
        })
        # Read back rather than trusting the values just written. A Firestore
        # write is durable the moment it returns, so this is the same row, and it
        # means the caller gets the stored defaults filled in.
        return UserModel.find_by_id(user.id)

    @staticmethod
    def set_role(user_id: int, role: UserRole) -> Optional[User]:
        store.update(COLLECTION, user_id, {'role': role.value})
        return UserModel.find_by_id(user_id)

    @staticmethod
    def _to_user(row) -> User:
        return User(
            id=int(row['id']),
            microsoft_id=row.get('microsoft_id'),
            email=row.get('email') or '',
            name=row.get('name') or '',
            roll_number=row.get('roll_number'),
            phone=row.get('phone'),
            role=UserRole(row.get('role') or UserRole.STUDENT.value),
            department=row.get('department') or 'CSE',
            is_active=bool(row.get('is_active', True)),
            created_at=row.get('created_at') or datetime.now(),
            updated_at=row.get('updated_at') or row.get('created_at') or datetime.now(),
        )