import functools
import hmac
import os
import re
import secrets
import uuid
from datetime import datetime

from flask import abort, current_app, flash, g, request, session
from werkzeug.utils import secure_filename

from app.models import UserRole
from app.models.user import UserModel

_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')

# Endpoints that must stay reachable without a CSRF token (they are the login
# entry points themselves and are already guarded by Microsoft's own flow).
CSRF_EXEMPT_ENDPOINTS = {'auth.dev_login'}

_MAGIC_BYTES = {
    'pdf': [b'%PDF-'],
    'jpg': [b'\xff\xd8\xff'],
    'jpeg': [b'\xff\xd8\xff'],
    'png': [b'\x89PNG\r\n\x1a\n'],
}


def csrf_token() -> str:
    """Return the session's CSRF token, creating it on first use."""
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def validate_csrf_token() -> bool:
    """Constant-time comparison of the submitted token against the session."""
    expected = session.get('_csrf_token')
    if not expected:
        return False
    submitted = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
    if not submitted:
        return False
    return hmac.compare_digest(str(expected), str(submitted))


def enforce_csrf() -> None:
    """Reject any state-changing request that lacks a valid CSRF token."""
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return
    if not validate_csrf_token():
        current_app.logger.warning(
            'CSRF rejection on %s %s', request.method, request.path
        )
        abort(400, description='Your session expired or the form was tampered with. '
                               'Please reload the page and try again.')


def rotate_csrf_token() -> None:
    """Issue a fresh token after a privilege change (sign-in, sign-out)."""
    session.pop('_csrf_token', None)


def session_idle_timeout_seconds() -> int:
    return int(current_app.config.get('IDLE_TIMEOUT_SECONDS', 3600))


def sanitize_filename(filename: str) -> str:
    """Strip directory components and unsafe characters from a client filename."""
    if not filename:
        raise ValueError('Missing filename')
    # Drop any path information (guards against ../ traversal).
    filename = os.path.basename(filename.replace('\\', '/'))
    filename = secure_filename(filename)
    if not filename:
        raise ValueError('Invalid filename')
    return filename


def file_extension(filename: str) -> str:
    """Return the lowercased extension without the dot."""
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def allowed_file(filename: str) -> bool:
    ext = file_extension(filename)
    return ext in current_app.config['ALLOWED_EXTENSIONS']


def is_valid_email(email: str) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


def email_domain(email: str) -> str:
    return email.rsplit('@', 1)[-1].lower() if '@' in email else ''


def is_university_email(email: str) -> bool:
    return email_domain(email) == current_app.config['DEV_ALLOWED_DOMAIN'].lower()


def sniff_extension(head: bytes) -> str:
    """Identify the real type from leading bytes, defeating renamed extensions."""
    for ext, signatures in _MAGIC_BYTES.items():
        for signature in signatures:
            if head.startswith(signature):
                return ext
    return ''


def generate_stored_filename(original_filename: str) -> str:
    """Random unguessable name; the original name never touches the filesystem."""
    ext = file_extension(original_filename)
    return f'{uuid.uuid4().hex}.{ext}' if ext else uuid.uuid4().hex


def request_referrer_path() -> str:
    return request.full_path if request.query_string else request.path


def relative_proof_dir(extension: str = '') -> tuple:
    """Bucket files by year/month as required by the storage layout."""
    from datetime import datetime
    now = datetime.now()
    return str(now.year), f'{now.month:02d}', extension


def current_user():
    """Load the signed-in user from session once per request.

    Enforces the idle timeout: a session that has been left untouched is
    discarded rather than silently granting continued access.
    """
    if 'user' in g:
        return g.user

    user_id = session.get('user_id')
    if user_id is None:
        return None

    last_seen = session.get('_last_seen')
    now = int(datetime.now().timestamp())
    idle_limit = session_idle_timeout_seconds()
    if last_seen and idle_limit and now - int(last_seen) > idle_limit:
        session.clear()
        rotate_csrf_token()
        return None

    user = UserModel.find_by_id(user_id)
    if user is None or not user.is_active:
        session.clear()
        rotate_csrf_token()
        return None

    session['_last_seen'] = now
    g.user = user
    return user


def login_required(view):
    """Require any authenticated user; remember where they were heading."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            from flask import redirect, url_for
            if session.get('_expired'):
                session.pop('_expired', None)
                flash('Your session timed out. Please sign in again.', 'warning')
            return redirect(url_for('auth.login', next=request_referrer_path()))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*allowed_roles: UserRole):
    """Allow only the listed roles through, otherwise 403."""
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = current_user()
            if user.role not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator