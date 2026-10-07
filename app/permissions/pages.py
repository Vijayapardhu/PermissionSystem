"""Page-level access control.

The routes used to carry their own role list inline (`@roles_required(HOD)`),
which meant the same decision lived in two places: the decorator on the route and
the navigation list in `app/utils/nav.py`. Nothing checked that the two agreed,
so a role could be handed a page in the sidebar that the route then refused, or
worse, a route could be added and quietly left open to every signed-in account.

So the grant moves here, once, as a page key. `PAGE_PERMISSIONS` is the only
place that says who may open what; the routes ask for a page key and the
navigation is derived from the same table, so the two cannot drift.

Two deliberate properties:

- A role absent from a page's tuple gets a 403, never a redirect and never a
  half-rendered page. The 403 handler in `app/__init__.py` already renders the
  styled error page with that wording.
- An endpoint that no page key names is a hole, so `_assert_every_endpoint_is_mapped`
  turns that into a failure at boot instead of a page that quietly opens up. The
  exemptions are listed by name rather than matched by prefix, so adding a route
  re-opens the check for it.
"""

import functools

from flask import abort

from app.models import UserRole
from app.utils.nav import nav_for
from app.utils.security import current_user, login_required

STUDENT = UserRole.STUDENT
LECTURER = UserRole.LECTURER
HOD = UserRole.HOD

ALL_ROLES = (STUDENT, LECTURER, HOD)
STAFF = (LECTURER, HOD)

# Endpoints reachable without a page grant, by name.
#
# The sign-in half of the flow has to work before anybody is signed in, and the
# public verification page is read by whoever holds the letter, which is the
# whole point of the QR code. `healthz` and `favicon.ico` are asked for by the
# platform rather than by a person, and `static` is the asset pipeline itself.
PUBLIC_ENDPOINTS = frozenset({
    'auth.landing',
    'auth.login',
    'auth.faculty_login',
    'auth.microsoft_login',
    'auth.callback',
    'auth.dev_login',
    'auth.logout',
    'auth.logout_all',
    'auth.verify_letter',
    'healthz',
    'favicon',
    'static',
})

# page key -> roles allowed to open it.
#
# Keys are endpoint names, so a grant and the route it governs read as the same
# string and there is no second naming scheme to keep in step.
PAGE_PERMISSIONS = {
    # Shared by every signed-in account; the sidebar links it as "Account".
    'auth.profile': ALL_ROLES,

    # ---- Student ----
    'student.dashboard': (STUDENT,),
    'student.new_request': (STUDENT,),
    # The directory lookup behind the member picker. Read-only and scoped to the
    # requester's own department inside the view, so the grant only has to say
    # "a student may search".
    'student.member_search': (STUDENT,),
    'student.requests': (STUDENT,),
    'student.withdraw': (STUDENT,),
    'student.classes_for_student': (STUDENT,),

    # ---- Faculty ----
    'faculty.dashboard': STAFF,
    'faculty.requests_browser': STAFF,
    'faculty.search': STAFF,
    'faculty.classes': STAFF,
    'faculty.create_class': STAFF,
    'faculty.class_detail': STAFF,
    'faculty.upload_roster': STAFF,
    'faculty.delete_member': STAFF,
    'faculty.delete_class': STAFF,
    'faculty.attendance': STAFF,
    'faculty.attendance_overview': STAFF,
    'faculty.reports': STAFF,

    # One faculty may only action and letter a request assigned to them, which is
    # a per-record check the view makes; the page grant is only the role gate.
    'faculty.action': (LECTURER,),
    'faculty.request_detail': STAFF,
    'faculty.request_letter': STAFF,
    'faculty.download_proof': ALL_ROLES,

    # Proctor lists: the rolls a lecturer mentors, kept apart from the classes
    # they teach. Same ownership rule as classes -- the view checks the owner.
    'faculty.proctor_students': STAFF,
    'faculty.create_proctor_group': STAFF,
    'faculty.proctor_group_detail': STAFF,
    'faculty.upload_proctor_roster': STAFF,
    'faculty.delete_proctor_member': STAFF,
    'faculty.delete_proctor_group': STAFF,

    # ---- HOD ----
    'hod.dashboard': (HOD,),
    'hod.requests': (HOD,),
    # The decision that actually grants a permission. HOD only, and deliberately
    # separate from the lecturer's `faculty.action` above.
    'hod.request_action': (HOD,),
    'hod.students': (HOD,),
    # Where the per-member decision is actually made.
    'hod.request_detail': (HOD,),
    'hod.faculty_workload': (HOD,),
    'hod.import_faculty': (HOD,),
    'hod.add_faculty': (HOD,),
    'hod.routing': (HOD,),
    'hod.classes': (HOD,),
    'hod.class_detail': (HOD,),
    # Department events and their coordinators, managed by the HOD.
    'hod.events': (HOD,),
    'hod.save_event': (HOD,),
    'hod.delete_event': (HOD,),
    'hod.print_report': (HOD,),
    'hod.reports': (HOD,),
    'faculty.reassign': (HOD,),

    # ---- Shared by student, lecturer and HOD ----
    #
    # All three open a request's detail page and print its letter. The letter is
    # not re-implemented per portal: both routes render `student/letter.html`, so
    # one grant has to cover both or the print button breaks for someone.
    'student.request_detail': ALL_ROLES,
    'student.request_letter': ALL_ROLES,
}


def _role_value(role):
    """A role as its stored string, whether it arrives as an enum or as text.

    `UserRole` is a plain Enum, so `UserRole.HOD == 'HOD'` is False and a grant
    table keyed by the enum silently refuses everyone when the caller happens to
    hold the role's string value -- which is what the navigation and the session
    row both carry. Comparing the `.value` on both sides makes the two spellings
    interchangeable at the boundary.
    """
    return getattr(role, 'value', role)


def page_allows(page: str, role) -> bool:
    """True when `role` holds the grant for `page`."""
    if role is None:
        return False
    wanted = _role_value(role)
    return any(_role_value(allowed) == wanted
               for allowed in PAGE_PERMISSIONS.get(page, ()))


def pages_for(user) -> frozenset:
    """Every page key the user may open. Empty for nobody signed in."""
    if user is None:
        return frozenset()
    return frozenset(
        page for page in PAGE_PERMISSIONS if page_allows(page, user.role)
    )


def can_access(user, page: str) -> bool:
    if user is None:
        return False
    return page_allows(page, user.role)


def visible_nav(role_value):
    """The navigation a role may actually open.

    Derived from the same table as the routes, so a page removed from a role's
    grant disappears from the sidebar in the same deploy that starts refusing it.
    """
    return [
        item for item in nav_for(role_value)
        if page_allows(item['endpoint'], role_value)
    ]


def permission_required(page: str):
    """Require a signed-in account holding the grant for `page`.

    Raises at decoration time when the key is not in the table. A typo in a page
    key would otherwise compile to a route nobody holds a grant for, which reads
    as "signed in but mysteriously refused" and is hard to trace back to a
    misspelling.
    """
    if page not in PAGE_PERMISSIONS:
        raise RuntimeError(
            f'No page permission is defined for {page!r}. Add it to '
            f'app.permissions.pages.PAGE_PERMISSIONS or the route will refuse '
            f'everybody.'
        )

    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = current_user()
            if not page_allows(page, user.role.value):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def unmapped_endpoints(url_map):
    """Registered endpoints that no page grant and no exemption covers."""
    return sorted(
        rule.endpoint for rule in url_map.iter_rules()
        if rule.endpoint not in PAGE_PERMISSIONS
        and rule.endpoint not in PUBLIC_ENDPOINTS
    )


def _assert_every_endpoint_is_mapped(app) -> None:
    """Fail the boot on an endpoint that no grant covers.

    The whole point of the table is that it is exhaustive. An endpoint added
    without a key is the one failure mode this module cannot catch at runtime --
    nobody asks for permission to a page the guard was never told about, so it
    would simply serve. Refusing to start turns that into a deploy error that
    names the endpoint, which is a far cheaper thing to discover than an
    internal page quietly reachable by every signed-in account.
    """
    missing = unmapped_endpoints(app.url_map)
    if missing:
        raise RuntimeError(
            'These endpoints have no page permission and are not exempt, so '
            'they would be open to any signed-in account. Add them to '
            'PAGE_PERMISSIONS or PUBLIC_ENDPOINTS: ' + ', '.join(missing)
        )
