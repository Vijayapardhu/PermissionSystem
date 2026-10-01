import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    
    # Database
    MYSQL_HOST = os.environ.get('MYSQL_HOST') or 'localhost'
    MYSQL_USER = os.environ.get('MYSQL_USER') or 'root'
    MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD') or ''
    MYSQL_DB = os.environ.get('MYSQL_DB') or 'cse_permission_system'
    MYSQL_PORT = int(os.environ.get('MYSQL_PORT') or 3306)
    
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
    
    # Session
    SESSION_TYPE = 'filesystem'
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
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'storage', 'proofs')
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB
    ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}
    
    # Email (Outlook SMTP)
    MAIL_SERVER = os.environ.get('MAIL_SERVER') or 'smtp.office365.com'
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER') or MAIL_USERNAME
    
    # App
    APP_NAME = 'CSE Permission & Leave Tracking System'
    UNIVERSITY_NAME = 'Aditya University'
    DEPARTMENT_NAME = 'Department of Computer Science & Engineering'

    # Dev mode: bypasses Microsoft Entra ID with a local role picker so the
    # system can be exercised before IT provisions the app registration.
    DEV_MODE = os.environ.get('DEV_MODE', 'true').lower() == 'true'
    DEV_ALLOWED_DOMAIN = os.environ.get('DEV_ALLOWED_DOMAIN', 'adityauniversity.in')