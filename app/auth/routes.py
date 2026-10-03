from urllib.parse import quote, urlparse

from datetime import datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.auth import microsoft
from app.models import UserRole
from app.models.user import UserModel
from app.permissions.pages import permission_required
from app.utils.security import (
    csrf_token, current_user, database_unavailable, is_university_email,
    is_valid_email, login_required, rotate_csrf_token,
)

auth = Blueprint('auth', __name__)

DASHBOARDS = {
    UserRole.STUDENT: 'student.dashboard',
    UserRole.LECTURER: 'faculty.dashboard',
    UserRole.HOD: 'hod.dashboard',
}

ALLOWED_ROLES = tuple(DASHBOARDS)


def _safe_next(target: str) -> str:
    """Only allow same-site relative redirects."""
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith('/'):
        return None
    return target


def _establish_session(user) -> None:
    """Start a fresh session, discarding any pre-existing CSRF token."""
    session.clear()
    rotate_csrf_token()
    session['user_id'] = user.id
    session['_last_seen'] = int(datetime.now().timestamp())
    session.permanent = True


def route_for_role(user) -> str:
    return url_for(DASHBOARDS[user.role])


@auth.route('/verify/<reference>')
def verify_letter(reference: str):
    """Public lookup for a scanned permission letter.

    Reachable without a session, because the whole point of the QR code is that
    a gatekeeper, a class rep or an employer can confirm a letter without an
    account. Access control is the signature in the URL (see app/utils/qr.py),
    not authentication, so this route deliberately grants no privilege: it shows
    what the letter already shows and nothing that only staff can see.

    A link that does not verify returns 404, the same as a request that does not
    exist. Answering 403 to a forged signature and 404 to a missing one would
    hand anyone a way to test which reference numbers are real.
    """
    from app.models.permission import (
        ApprovalModel, PermissionModel, attach_members,
    )
    from app.models.user import UserModel
    from app.utils.qr import reference_for, resolve_reference

    try:
        request_id = resolve_reference(reference)
    except ValueError:
        abort(404)

    try:
        record = PermissionModel.find_by_id(request_id)
        # A group letter is verified by the people standing in front of it, so
        # every student it covers has to be named here. Only the members are
        # exposed -- never who else was on the request or anything else private.
        if record is not None:
            attach_members([record])
    except Exception:
        current_app.logger.exception('Verification lookup failed for %s', reference)
        abort(503, description=(
            'The department record could not be reached. Please try again shortly.'
        ))

    if record is None:
        abort(404)

    student = UserModel.find_by_id(record.student_id)
    history = ApprovalModel.find_by_request(request_id)
    decision = history[-1] if history else None

    faculty_name = getattr(decision, 'faculty_name', None) if decision else None

    return render_template(
        'auth/verify.html',
        record=record,
        student=student,
        decision=decision,
        faculty_name=faculty_name,
        reference=reference_for(request_id),
    )


@auth.route('/')
def landing():
    user = current_user()
    if user:
        return redirect(route_for_role(user))
    if database_unavailable():
        # Signed out because the lookup could not be made, not because nobody is
        # signed in. Saying so keeps the portal's front door from reading as a
        # lost session every time the database is down.
        abort(503, description=(
            'The department records are temporarily unavailable. '
            'Please try again in a moment.'
        ))
    return redirect(url_for('auth.login'))


STUDENT_TAGLINE = (
    'Submit activity permission requests, track their approval, and print '
    'an official permission letter.'
)

FACULTY_TAGLINE = (
    'Verify student permission requests, manage your classes and '
    'attendance, and keep the department register accurate.'
)


@auth.route('/auth/login')
def login():
    if current_user():
        return redirect(route_for_role(current_user()))

    # csrf_token comes from the app context processor as a callable; passing a
    # snapshot string here would shadow it and break csrf_token() in templates.
    context = {
        'entra_available': microsoft.is_configured(),
        'dev_mode': current_app_dev_mode(),
        'tagline': STUDENT_TAGLINE,
    }
    if current_app_dev_mode():
        context['accounts'] = _dev_accounts()
    return render_template('auth/login.html', **context)


@auth.route('/auth/faculty-login')
def faculty_login():
    """Dedicated sign-in page for staff.

    Same Microsoft identity flow as the student sign-in, but the page is
    worded for lecturers and the HOD and returns them straight to their own
    dashboard instead of a student portal.
    """
    if current_user():
        return redirect(route_for_role(current_user()))

    return render_template(
        'auth/faculty_login.html',
        entra_available=microsoft.is_configured(),
        dev_mode=current_app_dev_mode(),
        faculty_accounts=_faculty_accounts(),
        tagline=FACULTY_TAGLINE,
    )


def _faculty_accounts():
    """Staff accounts offered by the local development picker."""
    from app.models import UserRole
    from app.models.firestore import store
    try:
        accounts = []
        for role in (UserRole.HOD.value, UserRole.LECTURER.value):
            rows = store.documents('users', role=role, is_active=True)
            for row in rows:
                accounts.append({
                    'email': row.get('email'),
                    'name': row.get('name'),
                    'role': row.get('role'),
                })
        accounts.sort(key=lambda a: a['name'] or '')
        return accounts
    except Exception:
        return []


@auth.route('/auth/microsoft')
def microsoft_login():
    """Hand the user off to the Microsoft Entra ID login page."""
    if not microsoft.is_configured():
        flash(
            'Microsoft Entra ID is not configured on this server.', 'danger'
        )
        return redirect(url_for('auth.login'))

    try:
        auth_url = microsoft.get_authorization_url()
    except microsoft.AuthConfigurationError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('auth.login'))

    return redirect(auth_url)


@auth.route('/auth/callback')
def callback():
    if request.args.get('error'):
        flash('Microsoft sign-in was cancelled or denied.', 'danger')
        return redirect(url_for('auth.login'))

    code = request.args.get('code')
    if not code:
        abort(400)

    result = microsoft.acquire_token_by_code(code)
    if 'error' in result:
        description = result.get('error_description') or result['error']
        flash(f'Sign-in failed: {description}', 'danger')
        return redirect(url_for('auth.login'))

    claims = result.get('id_token_claims') or {}
    email = (claims.get('email') or claims.get('preferred_username') or '').lower()
    name = claims.get('name') or email
    microsoft_id = claims.get('oid') or claims.get('sub')

    if not is_valid_email(email):
        flash('Your Microsoft account did not return a valid email address.', 'danger')
        return redirect(url_for('auth.login'))

    if not is_university_email(email):
        flash(
            f'Access is restricted to {current_app_domain()} accounts.',
            'danger',
        )
        return redirect(url_for('auth.login'))

    try:
        user = _require_user(UserModel.create_or_update_from_microsoft,
                             microsoft_id, email, name)
    except DirectoryUnavailable as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('auth.login'))

    if user.role not in ALLOWED_ROLES:
        flash('Your account is not provisioned for this portal.', 'danger')
        return redirect(url_for('auth.logout'))

    _establish_session(user)
    return redirect(_safe_next(request.args.get('next')) or route_for_role(user))


@auth.route('/auth/dev-login', methods=['POST'])
def dev_login():
    """Local sign-in used only while DEV_MODE is enabled.

    Mirrors the Entra path: an email is claimed and the role is resolved
    exclusively from the users table.
    """
    if not current_app_dev_mode():
        abort(404)

    email = (request.form.get('email') or '').strip().lower()
    if not is_valid_email(email) or not is_university_email(email):
        flash('Enter a valid university email address.', 'danger')
        return redirect(url_for('auth.login'))

    try:
        user = _require_user(UserModel.find_by_email, email)
    except DirectoryUnavailable as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('auth.login'))

    if user is None:
        flash(
            'That email is not registered. Ask the HOD to provision your account.',
            'danger',
        )
        return redirect(url_for('auth.login'))

    _establish_session(user)
    return redirect(_safe_next(request.form.get('next')) or route_for_role(user))


@auth.route('/account')
@permission_required('auth.profile')
def profile():
    """Read-only view of the signed-in identity and active sessions."""
    from app.models.permission import PermissionModel

    user = current_user()
    recent = PermissionModel.find_by_student(user.id, limit=5) if user.is_student else []

    return render_template(
        'auth/profile.html',
        user=user,
        recent=recent,
        idle_timeout_minutes=current_app.config['IDLE_TIMEOUT_SECONDS'] // 60,
    )


@auth.route('/auth/logout', methods=['GET', 'POST'])
def logout():
    """Sign out of the portal.

    GET only shows a confirmation page: signing out must never be triggerable by
    a link, an image tag or a prefetch, which is why the actual sign-out is POST
    behind a CSRF token. GET keeps bookmarked URLs and typed addresses usable
    instead of returning a bare 405.
    """
    if request.method == 'GET':
        # Do not pass current_user here: the context processor already exposes it
        # as a callable that base.html invokes.
        return render_template('auth/confirm_logout.html')

    session.clear()
    return redirect(url_for('auth.login'))


@auth.route('/auth/logout-all', methods=['POST'])
def logout_all():
    """Sign out of this app *and* end the Microsoft Entra session.

    Microsoft returns the user straight to the login page. No confirmation
    toast: signing out is unambiguous, and the banner only crowded the panel.
    """
    from app.auth import microsoft

    tenant = current_app_config().get('TENANT_ID')

    if tenant and microsoft.is_configured():
        session.clear()
        login_url = url_for('auth.login', _external=True)
        microsoft_logout = (
            f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/logout'
            f'?post_logout_redirect_uri={quote(login_url, safe="")}'
        )
        return redirect(microsoft_logout)

    session.clear()
    return redirect(url_for('auth.login'))


def current_app_config() -> dict:
    from flask import current_app
    return current_app.config


class DirectoryUnavailable(Exception):
    """Raised when the users table cannot be reached during sign-in."""


def _require_user(finder, *args):
    """Resolve a user or raise DirectoryUnavailable.

    Sign-in fails closed; this only converts an unreachable database into a
    readable message instead of a stack trace. The cause is logged at WARNING
    with the connection target attached, because a pool that cannot connect is
    an infrastructure problem and the traceback alone does not say which host or
    port was tried.
    """
    from flask import current_app
    try:
        return finder(*args)
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        current_app.logger.warning('User lookup failed during sign-in: %s', detail)
        current_app.logger.debug('User lookup traceback', exc_info=True)
        raise DirectoryUnavailable(
            'The user directory is temporarily unavailable. Please try again shortly.'
        )


def current_app_dev_mode() -> bool:
    from flask import current_app
    return current_app.config.get('DEV_MODE', False)


def current_app_domain() -> str:
    from flask import current_app
    return current_app.config['DEV_ALLOWED_DOMAIN']


def _dev_accounts():
    """Seed the dev picker with a representative account per role."""
    from app.models import UserRole
    from app.models.firestore import store
    try:
        # One representative per role, in the order the roles are listed above,
        # so the picker shows an HOD, a lecturer and a student.
        accounts = []
        for role in (UserRole.HOD.value, UserRole.LECTURER.value,
                     UserRole.STUDENT.value):
            rows = store.documents('users', role=role, is_active=True)
            rows.sort(key=lambda r: (r.get('roll_number') is None,
                                     r.get('roll_number') or ''))
            for row in rows[:4]:
                accounts.append({
                    'email': row.get('email'),
                    'name': row.get('name'),
                    'role': row.get('role'),
                    'roll_number': row.get('roll_number'),
                })
        return accounts[:12]
    except Exception:
        return []