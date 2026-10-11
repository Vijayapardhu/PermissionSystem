"""Create the Dean account the leave workflow needs.

The workflow could not finish before this: `hod_act_on_leave` moves an approved
leave to DEAN_PENDING and nothing moved it on. The college register the data was
imported from lists 214 lecturers and 5 heads of department and no Dean, because
the Dean of a school sits above the department rather than inside it -- so the
role exists in the app and the person does not exist in the data.

One row, idempotent: running it twice does not create a second Dean, and running
it against a database that already has one changes nothing. Replace the name,
email and employee code before applying this anywhere real -- the values below are
placeholders chosen to look like a real account in the demo list.
"""

import sys

sys.path.insert(0, r'C:\project\permission system')

from app.models import UserRole                      # noqa: E402
from app.models.firestore import store              # noqa: E402
from app.models.user import UserModel                # noqa: E402

EMAIL = 'dean.cse@adityauniversity.in'
NAME = 'DR. A. VENKATESWARA RAO'
EMPLOYEE_CODE = '5001'
DESIGNATION = 'Dean, School of Engineering'

USERS = 'users'


def main() -> int:
    existing = store.documents(USERS, email=EMAIL)
    if existing:
        row = existing[0]
        if row.get('role') == UserRole.DEAN.value:
            print(f'already a Dean: {NAME} ({EMAIL}) -- nothing to do')
            return 0
        UserModel.set_role(int(row['id']), UserRole.DEAN)
        store.update(USERS, int(row['id']), {
            'employee_code': EMPLOYEE_CODE, 'designation': DESIGNATION,
        })
        print(f'promoted the existing account {EMAIL} to Dean')
        return 0

    UserModel.create(microsoft_id=None, email=EMAIL, name=NAME,
                     role=UserRole.DEAN, department='CSE')
    dean = UserModel.find_by_email(EMAIL)
    if dean is not None:
        store.update(USERS, dean.id, {
            'employee_code': EMPLOYEE_CODE, 'designation': DESIGNATION,
        })
    print(f'created Dean: {NAME} <{EMAIL}> id={dean.id if dean else "?"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())