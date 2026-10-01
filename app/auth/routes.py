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
from app.utils.security import (
    csrf_token, current_user, is_university_email, is_valid_email, login_required,
    rotate_csrf_token,
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


@auth.route('/')
def landing():
    if current_user():
        return redirect(route_for_role(current_user()))
    return redirect(url_for('auth.login'))


STUDENT_TAGLINE = (
    'Submit leave and classroom permission requests, track your '
    'approval status, and download an official permission letter.'
)

STUDENT_POINTS = [
    'Request leave or classroom permission in a few steps',
    'Upload supporting proof for every request',
    'Track each decision from your lecturer',
    'Download a formal permission letter',
]

FACULTY_TAGLINE = (
    'Verify student permission requests, manage your classes and '
    'attendance, and keep the department register accurate.'
)

FACULTY_POINTS = [
    'Review and decide on assigned requests',
    'Search any student record in the department',
    'Build classes and import rosters from Excel',
    'Mark attendance, excused by approved permission',
]


@auth.route('/auth/login')
def login():
    if current_user():
        return redirect(route_for_role(current_user()))

    # Explain why the user is back here after Microsoft signed them out.
    signedout = request.args.get('signedout')
    if signedout == 'all':
        flash('You have been signed out of the portal and your Microsoft '
              'account sessions.', 'success')
    elif signedout == 'portal':
        flash('You have been signed out.', 'success')

    # csrf_token comes from the app context processor as a callable; passing a
    # snapshot string here would shadow it and break csrf_token() in templates.
    context = {
        'entra_available': microsoft.is_configured(),
        'dev_mode': current_app_dev_mode(),
        'tagline': STUDENT_TAGLINE,
        'points': STUDENT_POINTS,
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
        points=FACULTY_POINTS,
    )


def _faculty_accounts():
    """Staff accounts offered by the local development picker."""
    from app.models.database import db
    try:
        with db.get_cursor() as cursor:
            cursor.execute(
                """SELECT email, name, role FROM users
                   WHERE role IN ('LECTURER', 'HOD') AND is_active = TRUE
                   ORDER BY FIELD(role, 'HOD', 'LECTURER'), name"""
            )
            return cursor.fetchall()
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
@login_required
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
    flash('You have been signed out.', 'success')
    return redirect(url_for('auth.login', signedout='portal'))


@auth.route('/auth/logout-all', methods=['POST'])
def logout_all():
    """Sign out of this app *and* end the Microsoft Entra session.

    Microsoft returns the user to the login page (not to /auth/logout, which
    would only show an "already signed out" notice), with a marker so the login
    page can confirm what happened.
    """
    from app.auth import microsoft

    tenant = current_app_config().get('TENANT_ID')

    if tenant and microsoft.is_configured():
        session.clear()
        login_url = url_for('auth.login', _external=True, signedout='all')
        microsoft_logout = (
            f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/logout'
            f'?post_logout_redirect_uri={quote(login_url, safe="")}'
        )
        return redirect(microsoft_logout)

    session.clear()
    flash('You have been signed out of the portal.', 'success')
    return redirect(url_for('auth.login'))


def current_app_config() -> dict:
    from flask import current_app
    return current_app.config


class DirectoryUnavailable(Exception):
    """Raised when the users table cannot be reached during sign-in."""


def _require_user(finder, *args):
    """Resolve a user or raise DirectoryUnavailable.

    Sign-in fails closed; this only converts an unreachable database into a
    readable message instead of a stack trace.
    """
    from flask import current_app
    try:
        return finder(*args)
    except Exception:
        current_app.logger.exception('User lookup failed during sign-in')
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
    from app.models.database import db
    try:
        with db.get_cursor() as cursor:
            cursor.execute(
                """SELECT email, name, role, roll_number FROM users
                   WHERE is_active = TRUE
                   ORDER BY FIELD(role, 'HOD', 'LECTURER', 'STUDENT'),
                            roll_number IS NULL, roll_number
                   LIMIT 12"""
            )
            return cursor.fetchall()
    except Exception:
        return []