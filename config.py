import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'

    # Database: Supabase Postgres.
    # Use the Session-mode pooler (port 5432), not the transaction-mode pooler
    # (6543). psycopg promotes statements to server-side prepared statements,
    # which transaction mode discards between transactions.
    DATABASE_URL = os.environ.get('DATABASE_URL') or ''
    DB_POOL_MIN = int(os.environ.get('DB_POOL_MIN') or 1)
    DB_POOL_MAX = int(os.environ.get('DB_POOL_MAX') or 4)
    DB_CONNECT_TIMEOUT = int(os.environ.get('DB_CONNECT_TIMEOUT') or 10)

    # Microsoft Entra ID (Azure AD)
    CLIENT_ID = os.environ.get('CLIENT_ID')
    CLIENT_SECRET = os.environ.get('CLIENT_SECRET')
    TENANT_ID = os.environ.get('TENANT_ID')
    AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}" if TENANT_ID else None
    REDIRECT_URI = os.environ.get('REDIRECT_URI') or 'http://localhost:5000/auth/callback'
    # Identity only. MSAL appends offline_access, openid and profile itself; those
    # are reserved and must not be listed here. User.Read is deliberately absent:
    # it is a Microsoft Graph permission requiring tenant admin consent, and this
    # app never calls Graph. The OIDC email scope needs no separate consent.
    SCOPES = ['email']

    # Session.
    # Signed cookies, not a server-side store: Render's filesystem is ephemeral,
    # so a filesystem session dies on every redeploy and every idle spin-down.
    # A cookie session needs no extra service and survives both.
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    # Absolute cap; a session idle for longer than this is discarded on the
    # next request. Keeps a left-open browser from holding access indefinitely.
    IDLE_TIMEOUT_SECONDS = int(os.environ.get('IDLE_TIMEOUT_SECONDS') or 3600)

    # Cookie hardening. Set SESSION_COOKIE_SECURE=true in production once the
    # portal is served over HTTPS. It defaults to false so local http://localhost
    # development keeps working (browsers drop Secure cookies on plain HTTP).
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_NAME = 'cse_permission_session'
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true'

    # File Upload
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB
    ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}

    # Supabase Storage, which holds uploaded proof documents.
    # The bucket must be PRIVATE. Proofs carry medical and identity documents,
    # so they are only ever served back through an authorised Flask route.
    #
    # Supabase renamed the service-role key: it is now published as
    # SUPABASE_SECRET_KEY (sb_secret_...). The legacy service_role JWT is still
    # accepted under SUPABASE_SERVICE_ROLE_KEY for older projects.
    SUPABASE_URL = os.environ.get('SUPABASE_URL') or ''
    SUPABASE_SECRET_KEY = (
        os.environ.get('SUPABASE_SECRET_KEY')
        or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
        or ''
    )
    SUPABASE_STORAGE_BUCKET = os.environ.get('SUPABASE_STORAGE_BUCKET') or 'proofs'
    STORAGE_TIMEOUT_SECONDS = int(os.environ.get('STORAGE_TIMEOUT_SECONDS') or 30)

    # Email (Outlook SMTP)
    MAIL_SERVER = os.environ.get('MAIL_SERVER') or 'smtp.office365.com'
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER') or MAIL_USERNAME

    # Day-bucketing for charts and reports. Timestamps are stored as timestamptz
    # (UTC), but a request submitted at 23:30 IST belongs to that IST day, not
    # to the UTC day it may already have crossed.
    REPORT_TIMEZONE = os.environ.get('REPORT_TIMEZONE') or 'Asia/Kolkata'

    # App
    APP_NAME = 'CSE Permission & Leave Tracking System'
    UNIVERSITY_NAME = 'Aditya University'
    DEPARTMENT_NAME = 'Department of Computer Science & Engineering'

    # Dev mode: bypasses Microsoft Entra ID with a local role picker so the
    # system can be exercised before IT provisions the app registration.
    DEV_MODE = os.environ.get('DEV_MODE', 'true').lower() == 'true'
    DEV_ALLOWED_DOMAIN = os.environ.get('DEV_ALLOWED_DOMAIN', 'adityauniversity.in')

    # Development convenience, off in production.
    #
    # Without this, Jinja compiles each template once and keeps it for the life
    # of the process, while static files are read from disk on every request.
    # Editing a template then leaves the server serving old markup against new
    # CSS, which looks exactly like a broken layout and is very hard to spot.
    # TEMPLATES_AUTO_RELOAD makes the template loader re-check mtime, so a save
    # is picked up without a restart.
    TEMPLATES_AUTO_RELOAD = os.environ.get(
        'TEMPLATES_AUTO_RELOAD', 'false'
    ).lower() == 'true'
