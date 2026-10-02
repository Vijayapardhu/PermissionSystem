import os
from datetime import timedelta
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'

    # Data: Cloud Firestore.
    #
    # Firestore replaced Postgres rather than sitting beside it. The direct
    # Supabase host is IPv6-only and Render has no IPv6 route to it, so a
    # Postgres deployment there cannot open a connection at all; Firestore is
    # reached over ordinary HTTPS and needs no inbound connectivity either.
    #
    # Credentials are read from exactly one of:
    #   FIREBASE_CREDENTIALS_JSON - the service account JSON inline, which is
    #                               what a Render environment variable holds
    #   FIREBASE_CREDENTIALS_PATH - a path to that JSON on disk, for local runs
    # With neither, the app still boots and every data-backed page reports the
    # store as unavailable, which is the same shape as a database outage.
    FIREBASE_PROJECT_ID = os.environ.get('FIREBASE_PROJECT_ID') or ''
    FIRESTORE_DATABASE = os.environ.get('FIRESTORE_DATABASE') or '(default)'
    FIREBASE_CREDENTIALS_JSON = os.environ.get('FIREBASE_CREDENTIALS_JSON') or ''
    FIREBASE_CREDENTIALS_PATH = os.environ.get('FIREBASE_CREDENTIALS_PATH') or ''

    # Point at the Firestore emulator to develop offline, e.g. 127.0.0.1:8080.
    # The emulator ignores credentials entirely.
    FIRESTORE_EMULATOR_HOST = os.environ.get('FIRESTORE_EMULATOR_HOST') or ''

    # How long a single Firestore operation may take before the request gives up.
    # Two things used to share one budget: the pool checkout and the TCP connect.
    # There is no pool and no handshake here, so this is the whole cost of a
    # query that never answers. Kept short so a stuck call cannot hold a worker
    # thread for the gunicorn timeout.
    FIRESTORE_TIMEOUT_SECONDS = float(os.environ.get('FIRESTORE_TIMEOUT_SECONDS') or 8)

    # How many times a request retries a failed operation before giving up. A
    # single failure is not proof the store is gone: one RPC can be dropped or
    # timed out while the project is perfectly healthy. Two attempts means a blip
    # is survivable and a real outage still fails well inside the gunicorn
    # request timeout.
    FIRESTORE_RETRIES = int(os.environ.get('FIRESTORE_RETRIES') or 2)

    # How long a health probe may reuse the previous store verdict. Render polls
    # the health path continuously, and each probe was costing a query -- the
    # probe itself was part of the contention it detected.
    HEALTH_DB_CACHE_SECONDS = int(os.environ.get('HEALTH_DB_CACHE_SECONDS') or 30)
    HEALTH_DB_CACHE_SECONDS = int(os.environ.get('HEALTH_DB_CACHE_SECONDS') or 30)

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

    # Proof documents: Cloudflare R2, which holds uploaded proofs.
    #
    # The bucket must stay PRIVATE. Proofs carry medical and identity documents,
    # so they are only ever served back through an authorised Flask route; nothing
    # in this app ever hands out a public URL for one.
    #
    # R2 speaks the S3 API, so these are an R2 API token scoped to the bucket
    # ("Object Read & Write"), not AWS keys. Console -> R2 -> Manage R2 API
    # Tokens -> Create Account API token. It bypasses every bucket policy, so it
    # is a server-side secret: never in a template, a JS file, or any client-side
    # code. Rotate it if it is ever committed or pasted somewhere it should not be.
    STORAGE_BACKEND = (os.environ.get('STORAGE_BACKEND') or 'r2').lower()
    R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID') or ''
    R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID') or ''
    R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY') or ''
    R2_BUCKET = os.environ.get('R2_BUCKET') or 'permission-system'

    # Supabase Storage, kept for a deployment still storing proofs there.
    # Only read when STORAGE_BACKEND=supabase.
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

    # One budget for a whole storage round trip: TCP connect plus the read or
    # write. Long enough for a 5 MB proof over a slow link, short enough that a
    # hung request cannot hold a worker thread for the gunicorn timeout.
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

    # Static assets are immutable for a given file on disk, and the templates
    # cache-bust them with the file mtime (see _register_static_cachebusting).
    # That lets the browser keep them for a year instead of revalidating on
    # every navigation -- the transfer log was full of 304s for the same
    # stylesheet. A redeploy changes the mtime, which changes the URL, so a
    # stale asset cannot survive an update.
    SEND_FILE_MAX_AGE_DEFAULT = int(
        os.environ.get('SEND_FILE_MAX_AGE_SECONDS') or 31536000
    )

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
