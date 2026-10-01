import logging
import time

from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, render_template, request

from config import Config

load_dotenv()


def create_app(config_object=Config) -> Flask:
    app = Flask(
        __name__,
        template_folder='../templates',
        static_folder='../static',
    )
    app.config.from_object(config_object)

    logging.basicConfig(level=logging.INFO)
    app.logger.setLevel(logging.INFO)

    _register_extensions(app)
    _register_blueprints(app)
    _register_health_check(app)
    _register_static_cachebusting(app)
    _register_jinja_globals(app)
    _register_request_hooks(app)
    _register_template_helpers(app)
    _register_error_handlers(app)

    return app


def _register_health_check(app: Flask) -> None:
    """Liveness probe for Render.

    This deliberately does not fail when the database is slow.

    Render polls the health path continuously and restarts the service on a
    non-2xx, so a probe that pings the database turns a transient network
    hiccup into a restart loop. The container is restarted, every pooled
    connection is torn down and re-established, the first requests after boot
    queue behind that, and the next probe fails too. The site then looks
    uniformly slow, which is a far worse outcome than a page that errors.

    Restarting also cannot fix an unreachable database -- the replacement
    process has the same network path. So the probe reports what it sees and
    stays 200, and the verdict is cached for HEALTH_DB_CACHE_SECONDS so that
    polling does not itself consume a pooled connection. The response carries
    no detail about the cause, since the endpoint is unauthenticated.
    """

    state = {'checked_at': 0.0, 'database': 'unknown'}

    @app.route('/healthz')
    def healthz():
        from app.models.database import db

        now = time.monotonic()
        ttl = app.config['HEALTH_DB_CACHE_SECONDS']
        if now - state['checked_at'] >= ttl:
            try:
                db.ping()
                state['database'] = 'ok'
            except Exception:
                app.logger.exception('Health check: database unreachable')
                state['database'] = 'unreachable'
            state['checked_at'] = now

        return {'status': 'ok', 'database': state['database']}, 200


def _register_static_cachebusting(app: Flask) -> None:
    """Add `?v=<mtime>` to static URLs so they can be cached for a year.

    Flask serves /static/<path> with the file's mtime as the validator, so by
    default the browser revalidates the stylesheet and the logo on every single
    page view -- a full round trip per asset per navigation, which showed up in
    the transfer log as a wall of 304s.

    Long max-age alone would be wrong: the filenames are not hashed, so the URL
    for a changed stylesheet would be identical and browsers would keep serving
    the old copy for a year. Stamping the mtime into the query string makes the
    URL change exactly when the content does, so the asset can be cached
    aggressively and a deploy still takes effect immediately.

    Exposed to templates as `asset()`. Kept alongside the existing
    url_for('static', ...) calls rather than replacing them, so nothing depends
    on the helper being present.
    """
    from flask import url_for

    static_root = app.static_folder
    # Served in-process by gunicorn, but the origin in front of Render is
    # Cloudflare. A year is safe only because the URL carries the version.
    app.config.setdefault('SEND_FILE_MAX_AGE_DEFAULT', 31536000)

    @app.context_processor
    def _asset_in_context():
        import os as _os

        def asset(filename: str) -> str:
            version = 0
            try:
                version = int(_os.path.getmtime(
                    _os.path.join(static_root, filename)
                ))
            except OSError:
                # A missing file still needs a URL; without a version it just
                # falls back to the normal revalidating behaviour.
                pass
            return url_for('static', filename=filename, v=version or None)

        return {'asset': asset}


def _register_jinja_globals(app: Flask) -> None:
    """Expose a few builtins that Jinja sandboxes out by default."""
    import itertools

    app.jinja_env.globals.update({
        'zip': zip,
        'enumerate': enumerate,
        'pairwise': getattr(itertools, 'pairwise', None),
        'dict': dict,
        'range': range,
    })


def _register_extensions(app: Flask) -> None:
    from app.models.database import init_db
    from app.utils.email import mail

    try:
        init_db(app)
    except Exception as exc:  # pragma: no cover - surfaces misconfig clearly
        app.logger.error('Database pool unavailable: %s', exc)

    try:
        mail.init_app(app)
    except Exception as exc:  # pragma: no cover
        app.logger.error('Mail extension failed to initialise: %s', exc)


def _register_blueprints(app: Flask) -> None:
    from app.auth.routes import auth
    from app.faculty.routes import faculty_bp
    from app.hod.routes import hod_bp
    from app.student.routes import student_bp

    app.register_blueprint(auth)
    app.register_blueprint(student_bp)
    app.register_blueprint(faculty_bp)
    app.register_blueprint(hod_bp)


def _register_request_hooks(app: Flask) -> None:
    from app.utils.security import enforce_csrf

    @app.before_request
    def _csrf_guard():
        enforce_csrf()

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('X-XSS-Protection', '0')
        response.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
        response.headers.setdefault(
            'Permissions-Policy', 'geolocation=(), microphone=(), camera=()'
        )
        # Student records and proof filenames must never be cached by proxies.
        if request.path.startswith(('/student', '/faculty', '/hod')):
            response.headers.setdefault('Cache-Control', 'no-store')
        return response

    @app.context_processor
    def _csrf_in_context():
        # Expose the callable, not a snapshot string, so every template and
        # macro can write csrf_token() and always get the current value.
        from app.utils.security import csrf_token as _token
        return {'csrf_token': _token}


def _register_template_helpers(app: Flask) -> None:
    from app.utils.nav import SIDEBAR_ROLES
    from app.utils.security import current_user

    @app.context_processor
    def inject_globals():
        # Expose the path under its own name: templates pass a PermissionRequest
        # as `request`, which would shadow Flask's `request` proxy and break any
        # use of request.path in the layout.
        from app.utils.nav import group_sections, nav_for
        from app.utils.security import current_user as _current_user

        user = _current_user()
        items = nav_for(user.role.value) if user else []
        return {
            'APP_NAME': app.config['APP_NAME'],
            'UNIVERSITY_NAME': app.config['UNIVERSITY_NAME'],
            'DEPARTMENT_NAME': app.config['DEPARTMENT_NAME'],
            'DEV_ALLOWED_DOMAIN': app.config['DEV_ALLOWED_DOMAIN'],
            'now_date': datetime.now().strftime('%Y-%m-%d'),
            'current_user': _current_user,
            'current_path': request.path,
            'nav_items': items,
            'nav_sections': group_sections(items),
            'show_sidebar': bool(user) and user.role.value in SIDEBAR_ROLES,
            'status_classes': {
                'PENDING': 'bg-warning text-dark',
                'APPROVED': 'bg-success',
                'REJECTED': 'bg-danger',
                'CANCELLED': 'bg-secondary',
                'EXPIRED': 'bg-dark',
            },
            'status_icons': {
                'PENDING': 'hourglass-split',
                'APPROVED': 'check-circle',
                'REJECTED': 'x-circle',
                'CANCELLED': 'slash-circle',
                'EXPIRED': 'clock-history',
            },
        }


def _register_error_handlers(app: Flask) -> None:
    from werkzeug.exceptions import HTTPException

    @app.errorhandler(400)
    def bad_request(error):
        return render_template(
            'errors/error.html', code=400,
            message=getattr(error, 'description', None) or
                    'The request could not be understood.',
        ), 400

    @app.errorhandler(405)
    def method_not_allowed(error):
        # Reached only for genuine method mismatches on state-changing routes;
        # /auth/logout handles its own GET with a confirmation page.
        allowed = ', '.join(sorted(getattr(error, 'valid_methods', None) or []))
        message = 'This action is not available with that method.'
        if allowed:
            message += f' Allowed: {allowed}.'
        return render_template('errors/error.html', code=405, message=message), 405

    @app.errorhandler(403)
    def forbidden(error):
        return render_template(
            'errors/error.html', code=403,
            message='You do not have permission to view this page.',
        ), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template(
            'errors/error.html', code=404,
            message='The page you requested could not be found.',
        ), 404

    @app.errorhandler(413)
    def too_large(error):
        return render_template(
            'errors/error.html', code=413,
            message='The uploaded file is larger than the 5 MB limit.',
        ), 413

    @app.errorhandler(500)
    def server_error(error):  # pragma: no cover
        app.logger.exception('Unhandled server error')
        return render_template(
            'errors/error.html', code=500,
            message='An unexpected error occurred.',
        ), 500