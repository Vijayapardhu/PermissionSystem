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


def display_name_for_sign_in(stored_name: str, microsoft_name: str) -> str:
    """Which name survives a sign-in.

    The Outlook display name is very often the roll number itself, so writing
    it over the stored name on every sign-in would wipe a real name the
    department backfilled from the college register. A real stored name wins
    over a roll-shaped display name; anything else -- including a genuine
    rename in Outlook -- is taken as-is.
    """
    incoming = (microsoft_name or '').strip()
    stored = (stored_name or '').strip()
    if incoming and is_roll_id(incoming) and stored and not is_roll_id(stored):
        return stored
    return incoming or stored


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

    #: Everyone who teaches. A Deputy HOD is in it, because a deputy is a
    #: lecturer who was given delegated authority over requests -- they keep
    #: their classes, and must appear wherever a lecturer is picked.
    TEACHING_ROLES = (UserRole.LECTURER, UserRole.DEPUTY_HOD)

    @staticmethod
    def _by_role(role: str, order_by: str) -> List[User]:
        rows = store.documents(COLLECTION, role=role, is_active=True)
        users = [UserModel._to_user(row) for row in rows]
        # Sorting here rather than with order_by: Firestore would need a composite
        # index for a role filter plus an ordering, and a missing index is an
        # error at request time rather than a slower query.
        return sorted(users, key=lambda u: (getattr(u, order_by) or '', u.name))

    @staticmethod
    def _by_roles(roles, order_by: str) -> List[User]:
        """Every active user holding any of `roles`, as one sorted list.

        Reads each role separately because Firestore cannot filter on a list
        of values. The merge is sorted afterwards rather than concatenated:
        two individually sorted lists are not one sorted list, and a picker
        that interleaves two names for one person reads as a duplicate.
        """
        people = []
        for role in roles:
            people.extend(UserModel._by_role(role.value, order_by))
        return sorted(people, key=lambda u: (getattr(u, order_by) or '', u.name))

    @staticmethod
    def get_lecturers() -> List[User]:
        """Teaching staff below the HOD, so including any Deputy HOD.

        The HOD is excluded on purpose -- where the point is to pick someone
        who will not also be approving.
        """
        return UserModel._by_roles(UserModel.TEACHING_ROLES, 'name')

    @staticmethod
    def get_faculty() -> List[User]:
        """Everyone who teaches, HODs included, as one name-ordered list.

        For pickers and pick-free lists where the question is "could this person
        be doing this job" -- a request reviewer, an event coordinator, the
        person a default leave substitution is drawn from. Use `get_lecturers`
        where the point is to exclude the HOD instead.
        """
        return UserModel._by_roles(
            UserModel.TEACHING_ROLES + (UserRole.HOD,), 'name')

    @staticmethod
    def get_deans() -> List[User]:
        """The Deans -- the last signature on a faculty leave.

        Kept out of `get_faculty` on purpose. Every picker that asks "could this
        person be doing this job" wants somebody who can hold a class, and a Dean
        does not in this portal; putting them there would offer a Dean as a
        substitute for a class they do not teach.
        """
        return UserModel._by_role(UserRole.DEAN.value, 'name')

    @staticmethod
    def get_students() -> List[User]:
        return UserModel._by_role(UserRole.STUDENT.value, 'roll_number')

    @staticmethod
    def get_hods() -> List[User]:
        return UserModel._by_role(UserRole.HOD.value, 'name')

    @staticmethod
    def create_or_update_from_microsoft(microsoft_id: str, email: str, name: str) -> User:
        """Provision or refresh a user from a verified Microsoft identity.

        A student imported from the college roster has a roll number and no email,
        because the mailbox they will sign in with does not exist in the register
        this app reads. Both lookups above miss on the first sign-in -- no
        Microsoft id, no stored address -- so without the roll number fallback
        here every one of them would arrive to an empty account, and a second
        sign-in would find that new one by Microsoft id and stop there. The
        imported record would sit beside it forever, half-populated, holding the
        roster's roll number while a duplicate held the real identity.

        Matching on the roll number the mailbox is named after is what links them.
        It has to come before creating anything, and it is deliberately last in
        the chain: an identity Microsoft has already vouched for, by id or by
        address, always wins over a name-shaped guess.
        """
        roll_number = derive_roll_number(email, name)

        existing = UserModel.find_by_microsoft_id(microsoft_id)
        if existing:
            existing.email = email.lower()
            existing.name = display_name_for_sign_in(existing.name, name)
            if roll_number:
                existing.roll_number = roll_number
            return UserModel.update(existing)

        existing = UserModel.find_by_email(email)
        if existing:
            existing.microsoft_id = microsoft_id
            existing.name = display_name_for_sign_in(existing.name, name)
            if roll_number:
                existing.roll_number = roll_number
            return UserModel.update(existing)

        if roll_number:
            existing = UserModel.find_by_roll_number(roll_number)
            if existing:
                existing.microsoft_id = microsoft_id
                existing.email = email.lower()
                # The imported row is named after the roll, which is what the
                # Outlook display name also is, so this keeps the real name the
                # moment one exists rather than pinning the roll to it forever.
                existing.name = display_name_for_sign_in(existing.name, name)
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
            # Present only on staff imported from the college register. Absent on
            # every row written before that import, and on every student, which is
            # why both fields default to None rather than to ''.
            employee_code=row.get('employee_code'),
            designation=row.get('designation'),
        )