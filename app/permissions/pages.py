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
DEPUTY_HOD = UserRole.DEPUTY_HOD
DEAN = UserRole.DEAN

ALL_ROLES = (STUDENT, LECTURER, HOD, DEPUTY_HOD, DEAN)
STAFF = (LECTURER, HOD, DEPUTY_HOD)

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
    'auth.demo_logins',
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
    # Renaming a class. The confirmed write is the same page key family; both are
    # gated on the class being the lecturer's own, which the route enforces.
    'faculty.edit_class': STAFF,
    'faculty.apply_class_edit': STAFF,
    'faculty.upload_roster': STAFF,
    'faculty.delete_member': STAFF,
    'faculty.delete_class': STAFF,
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
    # Adding one student to a proctor list by hand, beside the spreadsheet
    # upload. Split from `upload_proctor_roster` because the two differ in how
    # much they can change: the upload rewrites the list in bulk, this names one
    # student, and granting them the same key would widen the bulk write.
    'faculty.add_proctor_student': STAFF,
    # The search that box uses. Its own key so a lecturer can be given the
    # lookup without the write.
    'faculty.proctor_student_search': STAFF,
    'faculty.proctor_group_detail': STAFF,
    'faculty.upload_proctor_roster': STAFF,
    'faculty.delete_proctor_member': STAFF,
    'faculty.delete_proctor_group': STAFF,

    # Faculty leave and class adjustment. Staff only -- a student has no leave to
    # apply for and no class to adjust.
    #
    # The detail page carries the per-record check: a lecturer may only open and
    # act on their own application, and the view refuses somebody else's with a
    # 403. These grants are the role gate only, as with the classes above.
    'faculty.my_leave': STAFF,
    'faculty.apply_leave': STAFF,
    'faculty.my_leave_detail': STAFF,
    # Amending a leave is the applicant's own record, so the same gate as
    # viewing it. The HOD has their own approval screen and is not given this.
    # Two endpoints: the form, and the confirmed write.
    'faculty.edit_leave': STAFF,
    'faculty.apply_leave_edit': STAFF,
    'faculty.confirm_periods': STAFF,
    'faculty.request_substitution': STAFF,
    'faculty.withdraw_substitution': STAFF,
    'faculty.mark_uncovered': STAFF,
    'faculty.submit_leave': STAFF,
    'faculty.cancel_leave': STAFF,
    # The applicant's own approval letter. Its own key, separate from the HOD's
    # and the Dean's copies of the same document: the gate on each is different
    # (own record vs. any record vs. any record), and sharing one would mean
    # widening the applicant's grant to reach everybody's letters.
    'faculty.leave_letter': STAFF,
    # Answering somebody else's request is the one write here that touches a row
    # the signed-in person did not create, so it is granted separately and the
    # service checks the request was actually addressed to them.
    'faculty.my_adjustments': STAFF,
    'faculty.respond_adjustment': STAFF,

    # Classwork, and the objections it raises against permissions this lecturer
    # granted. STAFF because an HOD teaches as well as administering, and their
    # sidebar carries the teaching panel too.
    #
    # `faculty.objections` is a queue of other people's disputes about decisions
    # the signed-in person made, so it is granted to teaching staff but the view
    # only ever returns the entries addressed to them -- being able to open the
    # page does not mean being able to read someone else's, and the grant is the
    # role gate only.
    'faculty.classwork': STAFF,
    'faculty.create_classwork': STAFF,
    # Editing and removing are separate grants from creating, so they can be
    # given out separately without a second table entry later. Both are scoped
    # to the signed-in person's own entries by `_my_entry`, which 404s on
    # anybody else's -- the grant is the role gate, the ownership check is the
    # data gate, and the two are not the same thing.
    #
    # Note what editing now means. Moving a date or a session withdraws the
    # objections the old version raised and may open new ones, which suspends
    # permissions. So this is not a cosmetic edit and is not folded into
    # `faculty.classwork`.
    'faculty.edit_classwork': STAFF,
    # Named after the endpoint, not after a description. An earlier attempt at
    # this table used `faculty.delete_classwork`, and the boot-time check caught
    # the mismatch rather than leaving the route quietly ungated.
    'faculty.classwork_delete': STAFF,
    # Reading one entry, and the GET half of the edit form, are the same gate as
    # the list they sit behind -- `faculty.classwork` above. Separate keys for
    # them would mean three places to change when the rule moves.
    'faculty.classwork_detail': STAFF,
    'faculty.classwork_new': STAFF,
    'faculty.classwork_edit': STAFF,
    'faculty.classwork_edit_apply': STAFF,
    'faculty.objections': STAFF,
    # The write that decides a disputed permission, granted separately for the
    # same reason as `faculty.action` above: it changes the status of a request
    # the signed-in person did not create.
    'faculty.respond_objection': STAFF,

    # ---- HOD ----
    'hod.dashboard': (HOD,),
    'hod.requests': (HOD,),
    # The decision that actually grants a permission. HOD only, and deliberately
    # separate from the lecturer's `faculty.action` above.
    'hod.request_action': (HOD,),
    'hod.students': (HOD,),
    'hod.search': (HOD,),
    # Where the per-member decision is actually made.
    'hod.request_detail': (HOD,),
    'hod.faculty_workload': (HOD,),
    'hod.import_faculty': (HOD,),
    'hod.add_faculty': (HOD,),
    # Making a lecturer a Deputy HOD, and taking it back. HOD only, and not
    # shared with anything else: this is the one write that changes what another
    # person is allowed to do, so it gets its own key rather than riding on a
    # general faculty-administration grant.
    'hod.set_deputy': (HOD,),
    'hod.routing': (HOD,),
    # The confirmed write for the routing settings, same gate as the form.
    'hod.apply_routing': (HOD,),
    # Review desks: adding, editing and removing a named route. Three separate
    # keys rather than one, so a future grant that can create a desk does not
    # silently also carry the ability to delete one.
    'hod.route_create': (HOD,),
    'hod.route_save': (HOD,),
    'hod.route_delete': (HOD,),
    # The HOD deciding a request that is still in somebody else's queue, because
    # the assigned reviewer is unavailable. Same grant as `hod.request_action`
    # deliberately: both write the status of a request, and splitting them would
    # only create a distinction nobody can act on.
    'hod.decide_request_directly': (HOD,),
    'hod.classes': (HOD,),
    'hod.class_detail': (HOD,),
    # Department events and their coordinators, managed by the HOD.
    'hod.events': (HOD,),
    'hod.event_detail': (HOD,),
    'hod.edit_event': (HOD,),
    'hod.apply_event_edit': (HOD,),
    'hod.save_event': (HOD,),
    'hod.delete_event': (HOD,),
    # Faculty leave approvals. Separate from `faculty.leave`, which is a
    # lecturer's own applications: this is the queue of other people's, and the
    # two must not share a grant or a lecturer would see their own list through
    # the approver's key.
    'hod.leave_queue': (HOD,),
    'hod.leave_detail': (HOD,),
    # The decision that approves a faculty leave.
    'hod.leave_action': (HOD,),
    # The HOD's copy of the approval letter. Same document as the applicant's and
    # the Dean's, but reachable under the HOD's own key.
    'hod.leave_letter': (HOD,),
    'hod.print_report': (HOD,),
    'hod.reports': (HOD,),
    'faculty.reassign': (HOD,),

    # ---- Deputy HOD ----
    #
    # Two powers and no more: decide a request without waiting for its reviewer,
    # and read who currently holds a permission.
    #
    # Deliberately NOT in STAFF for the administrative panels. A deputy who could
    # open the faculty register, reconfigure routing or create events would be a
    # second HOD, and two people who both think they own the process is how a
    # department ends up with nobody accountable for it. What a deputy cannot
    # reach is the point as much as what they can.
    #
    # The leave queue below is the third power, and it is deliberately *not*
    # `hod.leave_queue`: a grant on the HOD's own page would carry the letter
    # link and the full administrative set with it.
    'deputy.dashboard': (DEPUTY_HOD,),
    'deputy.permitted': (DEPUTY_HOD,),
    'deputy.request_detail': (DEPUTY_HOD,),
    # The grant itself. Shares its service with `hod.request_action`, and the
    # self-approval refusal in that service applies here unchanged. Named after
    # the endpoint rather than after a description: the page table is keyed by
    # endpoint name, and the boot-time check is what catches a mismatch here
    # rather than leaving the route quietly open.
    'deputy.decide': (DEPUTY_HOD,),
    # Faculty leave, in the HOD's place. A Deputy HOD is an approver: they read
    # the same HOD_PENDING queue the HOD does and clear it, because an HOD for a
    # whole department means the queue stops moving whenever they are in a
    # meeting.
    #
    # What they cannot do is not expressed here, because it is not a role gate.
    # Their own application is refused by applicant id inside the leave service,
    # and an HOD's application never enters this queue at all -- `_next_approver_stage`
    # sends it straight to the Dean. Widening the grant cannot widen either,
    # which is the point of putting them there rather than in this table.
    'deputy.leave_queue': (DEPUTY_HOD,),
    'deputy.leave_detail': (DEPUTY_HOD,),
    'deputy.leave_action': (DEPUTY_HOD,),

    # ---- Dean ----
    #
    # The last signature on a faculty leave. Deliberately not in STAFF: a Dean
    # decides leave and does not administer a department here, so the queue is the
    # whole panel.
    'dean.dashboard': (DEAN,),
    'dean.leave_queue': (DEAN,),
    'dean.leave_detail': (DEAN,),
    # The grant itself. FINAL_APPROVED is the only state that makes a leave take
    # the applicant out of teaching and the only one a letter may be printed from,
    # so this is the endpoint the whole workflow exists to reach.
    'dean.leave_action': (DEAN,),
    'dean.leave_letter': (DEAN,),

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
