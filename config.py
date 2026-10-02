import os
from datetime import timedelta
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'

    # Database: Supabase Postgres, DIRECT connection.
    #
    # The direct host is db.<ref>.supabase.co. Nothing in this app derives a
    # second endpoint from it: DATABASE_URL is dialled exactly as written, with
    # no pooler host, no startup probe and no fallback, so the target in the
    # startup log is the URI the operator configured.
    #
    # Supavisor, the pooler, is deliberately not used. It reaps and reschedules
    # session connections underneath the client, which is what produced the
    # production log's "consuming input failed: SSL SYSCALL error: EOF
    # detected", and it shares one slot limit between every client pointed at
    # the project. This app pools for itself, so there is nothing for Supavisor
    # to pool.
    #
    # db.<ref>.supabase.co is published as an IPv6-only AAAA record, so the host
    # running the app needs IPv6 egress to resolve it. The pool opens in the
    # background either way, so an unresolvable host still boots and recovers on
    # its own rather than failing every request until the next deploy.
    #
    # Set DATABASE_URL to override all of this. Otherwise the URI is assembled
    # from the project ref and the password below, which keeps the password out
    # of two places at once.
    SUPABASE_PROJECT_REF = (
        os.environ.get('SUPABASE_PROJECT_REF') or 'cndqajpjmnvsafrxjowx'
    )
    SUPABASE_DB_PASSWORD = os.environ.get('SUPABASE_DB_PASSWORD') or ''
    DATABASE_URL = os.environ.get('DATABASE_URL') or (
        f'postgresql://postgres:{quote(SUPABASE_DB_PASSWORD, safe="")}'
        f'@db.{SUPABASE_PROJECT_REF}.supabase.co:5432/postgres'
        if SUPABASE_DB_PASSWORD else ''
    )

    # Sizing: the pool is per gunicorn worker process, so the total is
    # DB_POOL_MAX x workers, and it is charged against the Supabase project's
    # connection limit. It must also be at least the number of --threads that
    # worker serves, or a worker deadlocks against itself: every thread holds a
    # connection while a query runs, and the pool checkout then blocks forever.
    #
    # DB_POOL_MIN is what makes that survivable rather than merely unlikely.
    # It is the number of connections the pool holds ready, and the pool grows
    # only one at a time, on demand, once a client is already queued. Over a
    # WAN a single connect costs well over a second, so a pool that starts at
    # one and loses that one has nothing to serve for the whole connect, and
    # every request behind it times out against DB_POOL_TIMEOUT. Sitting at
    # min = --threads means each thread finds a warm connection instead of
    # queueing behind a dial. The pool raises a floor set below --threads up to
    # --threads itself and logs that it did, because the value actually running
    # is whatever the dashboard says.
    #
    # 2 workers x 4 threads, so 4 warm and 6 available per process: 12 at the
    # top end. Keep DB_POOL_MAX x DB_WORKER_COUNT inside the project's
    # connection limit, or lower --threads rather than raising the limit.
    DB_POOL_MIN = int(os.environ.get('DB_POOL_MIN') or 4)
    DB_POOL_MAX = int(os.environ.get('DB_POOL_MAX') or 6)

    # What one worker serves, and how many there are. Read by the pool only to
    # size itself and to state the resulting connection budget in the startup
    # log. These must match the gunicorn flags in Procfile / render.yaml.
    DB_WORKER_THREADS = int(os.environ.get('DB_WORKER_THREADS') or 4)
    DB_WORKER_COUNT = int(os.environ.get('DB_WORKER_COUNT') or 2)

    # These are deliberately different, and both short.
    #
    # DB_CONNECT_TIMEOUT is the TCP connect timeout. DB_POOL_TIMEOUT is how long
    # a request waits for a free connection. They used to be one 10s value, so
    # an exhausted pool made every request hang for ten seconds before it
    # failed -- users saw a frozen page and then a "temporarily unavailable"
    # message. Failing fast turns a stall into an error the UI can recover from,
    # and frees the worker for the next request.
    DB_CONNECT_TIMEOUT = int(os.environ.get('DB_CONNECT_TIMEOUT') or 5)
    DB_POOL_TIMEOUT = int(os.environ.get('DB_POOL_TIMEOUT') or 5)
    # How many times a request tries for a connection before giving up. A single
    # failed attempt is not proof the database is gone: the pool may have been
    # serving a socket that died, or one connect may have been dropped. Two
    # attempts means a blip is survivable and a real outage still fails well
    # inside the gunicorn request timeout.
    DB_POOL_RETRIES = int(os.environ.get('DB_POOL_RETRIES') or 2)

    # Pool housekeeping. Retiring an idle connection after four minutes means the
    # pool only ever hands out a socket the server still has.
    DB_POOL_MAX_IDLE = float(os.environ.get('DB_POOL_MAX_IDLE') or 240)
    DB_POOL_MAX_LIFETIME = float(os.environ.get('DB_POOL_MAX_LIFETIME') or 1800)
    # How long a pooled connection may be handed out without being verified
    # against the server. A socket dropped while idle still looks alive to libpq
    # -- closed is False, status is IDLE -- so it is only found dead when a query
    # goes out on it. This bounds how often that check can catch it. Thirty
    # seconds costs one SELECT 1 per idle gap and none at all on a hot path; set
    # it to 0 to fall back to libpq's own view, which cannot see a severed flow.
    DB_POOL_PING_INTERVAL = float(os.environ.get('DB_POOL_PING_INTERVAL') or 30)
    # How long the background worker keeps retrying to refill the pool to
    # min_size after a failure. Short, so the site recovers on its own once
    # Supabase does, instead of staying broken until the next deploy.
    DB_POOL_RECONNECT_TIMEOUT = float(
        os.environ.get('DB_POOL_RECONNECT_TIMEOUT') or 30
    )

    # How long a health probe may reuse the previous database verdict. Render
    # polls the health path continuously, and each probe was taking a pooled
    # connection -- the probe itself was part of the contention it detected.
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
