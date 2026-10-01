import msal
from flask import current_app, url_for

AUTHORITY_HOST = 'https://login.microsoftonline.com'

PLACEHOLDER_MARKERS = ('PASTE_', 'YOUR_', 'CHANGE_ME', 'XXXX', 'REPLACE_')


class AuthConfigurationError(RuntimeError):
    """Raised when the Entra app registration is not configured."""


def _is_placeholder(value) -> bool:
    """True when a setting still holds an unfilled template value."""
    if not value:
        return True
    upper = str(value).upper()
    return any(marker in upper for marker in PLACEHOLDER_MARKERS)


def _settings() -> dict:
    config = current_app.config
    missing = [
        name for name in ('CLIENT_ID', 'CLIENT_SECRET', 'TENANT_ID')
        if _is_placeholder(config.get(name))
    ]
    if missing:
        raise AuthConfigurationError(
            'Microsoft Entra ID is not configured; missing: ' + ', '.join(missing)
        )
    return {
        'client_id': config['CLIENT_ID'],
        'client_secret': config['CLIENT_SECRET'],
        'authority': f'{AUTHORITY_HOST}/{config["TENANT_ID"]}',
    }


def is_configured() -> bool:
    """True only when every setting holds a real value.

    An unfilled placeholder in .env counts as unconfigured so the login page
    explains the setup instead of sending the user to a page that cannot succeed.
    """
    return not any(
        _is_placeholder(current_app.config.get(key))
        for key in ('CLIENT_ID', 'CLIENT_SECRET', 'TENANT_ID')
    )


def _cache_key() -> str:
    return 'msal_token_cache'


def build_msal_app():
    """Confidential client backed by a per-session token cache."""
    settings = _settings()
    return msal.ConfidentialClientApplication(
        settings['client_id'],
        authority=settings['authority'],
        client_credential=settings['client_secret'],
    )


def get_msal_app():
    from flask import session
    app = build_msal_app()
    cache = msal.SerializableTokenCache()
    if session.get(_cache_key()):
        cache.deserialize(session[_cache_key()])
    app.token_cache = cache
    return app


def persist_cache(app) -> None:
    from flask import session
    if app.token_cache.has_state_changed:
        session[_cache_key()] = app.token_cache.serialize()


def scopes() -> list:
    return current_app.config['SCOPES']


def redirect_uri() -> str:
    """The redirect URI sent to Entra ID.

    The configured value wins: Entra ID matches this string exactly, so it must
    not be inferred from the inbound request. Deriving it from the Host header
    drops the port behind a reverse proxy and produces an AADSTS50011 mismatch.
    """
    configured = current_app.config.get('REDIRECT_URI')
    if configured:
        return configured
    return url_for('auth.callback', _external=True)


def get_authorization_url() -> str:
    return get_msal_app().get_authorization_request_url(
        scopes(), redirect_uri=redirect_uri()
    )


def acquire_token_by_code(auth_code: str) -> dict:
    app = get_msal_app()
    result = app.acquire_token_by_authorization_code(
        auth_code, scopes=scopes(), redirect_uri=redirect_uri()
    )
    persist_cache(app)
    return result


def sign_out() -> None:
    from flask import session
    session.clear()