"""QR codes and the public verification link on the permission letter.

The code carries a URL, not the record itself. Two reasons:

* The payload stays around ninety characters, so the symbol needs a low error
  correction level and survives being printed on a laser printer at letter size
  and then scanned off a photocopy.
* The page behind it renders properly on a phone and can be re-checked later,
  which a 30 mm square of dots cannot.

The signature in that URL is the access control, and it is worth being explicit
about why it is there. Anyone who scans the code is showing the same person the
same data the paper already carries, which is the intent. What must not be
possible is *guessing* a URL: a bare /verify/REQ-0001 would let anyone walk the
whole department, and these records carry medical reasons. The token is derived
from SECRET_KEY, so only this app can mint a working link.

The token is deliberately not timestamped. A printed letter has to keep
verifying for as long as it exists, and reissuing it per request would mean a
letter scanned tomorrow no longer matched the one printed today. The cost is
that rotating SECRET_KEY invalidates every letter already in circulation.
"""

import base64
import io
import logging

from urllib.parse import urlsplit

from flask import current_app, request, url_for
from itsdangerous import BadSignature, URLSafeSerializer

log = logging.getLogger('app.qr')

# Distinct salt, so a signature minted here can never be replayed against
# anything else that signs with the same SECRET_KEY (the session cookie, the
# CSRF token).
_SALT = 'permission-letter-verify'

# M is enough: the page behind the code is re-fetchable, so a smudged corner can
# be rescanned rather than reconstructed. H would bloat the symbol by two
# versions for no practical gain on a printed page.
_ERROR_CORRECTION = 'M'
# Six pixels per module: a 29 mm print at 300 dpi is ~343 px across, so this
# lands near 200 px and stays crisp without bloating the HTML.
_BOX_SIZE = 6
# The quiet zone. Scanners need it and the specification requires four modules;
# without it a code sitting flush against a table edge will not scan.
_BORDER = 4


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config['SECRET_KEY'], salt=_SALT)


def reference_for(request_id: int) -> str:
    """The human-facing reference, e.g. 42 -> 'REQ-0042'."""
    return f'REQ-{int(request_id):04d}'


def public_base_url() -> str:
    """The https origin that verification links are minted against.

    Render terminates TLS in front of gunicorn and nothing in this stack rewrites
    wsgi.url_scheme, so url_for(_external=True) emits http:// here -- and an
    http link printed on a formal letter is wrong in a way a user cannot fix.
    REDIRECT_URI is already the deployed origin, so reuse it rather than
    inferring a scheme from a proxied Host header.

    The fallback exists for deployments that never set REDIRECT_URI (it is only
    needed for Entra ID). There the forwarded scheme is trusted, because the
    alternative is minting a link that downgrades the one page a stranger is
    invited to open.
    """
    configured = current_app.config.get('REDIRECT_URI')
    if configured:
        parts = urlsplit(configured)
        if parts.scheme in ('http', 'https') and parts.netloc:
            return f'{parts.scheme}://{parts.netloc}'

    forwarded = request.headers.get('X-Forwarded-Proto', '')
    scheme = forwarded.split(',')[0].strip().lower()
    if scheme not in ('http', 'https'):
        scheme = request.scheme
    host = request.headers.get('X-Forwarded-Host', '').split(',')[0].strip()
    return f'{scheme}://{host or request.host}'.rstrip('/')


def verification_token(request_id: int) -> str:
    """The signed half of the link.

    The reference itself is signed rather than the bare id, so the readable part
    of the URL cannot be edited independently of the token. Note that
    itsdangerous joins the payload and the signature with a dot of its own, so
    the token is not a single dot-free segment and the two must be split on the
    *first* dot.
    """
    return _serializer().dumps(reference_for(request_id))


def verification_url(request_id: int) -> str:
    """The link the QR code points at, e.g. https://host/verify/REQ-0042.<token>."""
    reference = f'{reference_for(request_id)}.{verification_token(request_id)}'
    return f'{public_base_url()}{url_for("auth.verify_letter", reference=reference)}'


def resolve_reference(reference: str) -> int:
    """Return the request id a verification link names, or raise ValueError.

    404ing on a bad link keeps the endpoint from distinguishing "no such
    request" from "forged signature", which would otherwise be a free oracle for
    testing which reference numbers exist.
    """
    claimed, separator, token = (reference or '').partition('.')
    if not separator or not token:
        raise ValueError('malformed verification link')

    try:
        signed = _serializer().loads(token)
    except (BadSignature, ValueError, TypeError):
        raise ValueError('signature does not match') from None

    # The readable half is cosmetic, so it has to agree with what was signed or
    # a link could read REQ-0001 while resolving to a different record.
    if signed != claimed:
        raise ValueError('reference does not match the signature')

    digits = claimed.removeprefix('REQ-')
    if not digits.isdigit():
        raise ValueError('reference is not a request number')
    request_id = int(digits)
    if request_id < 1:
        raise ValueError('reference is out of range')
    return request_id


def data_uri(payload: str) -> str:
    """Render a payload as an inline PNG data URI.

    Inline rather than a file or a route: a letter prints as one document, and a
    second request for an image that never changes would only add a failure mode
    between the scan and the record it is supposed to prove.

    Returns an empty string if the encoder is unavailable, so a broken QR
    dependency degrades to a letter without a code rather than a letter that
    will not print at all.
    """
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError:  # pragma: no cover - deployment must pin the package
        log.warning('qrcode is not installed; the letter prints without a code')
        return ''

    code = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=_BOX_SIZE,
        border=_BORDER,
    )
    code.add_data(payload)
    code.make(fit=True)

    buffer = io.BytesIO()
    code.make_image(fill_color='black', back_color='white').save(
        buffer, format='PNG'
    )
    encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
    return f'data:image/png;base64,{encoded}'


def letter_qr(request_id: int) -> dict:
    """Everything the letter template needs to draw and label its code."""
    url = verification_url(request_id)
    return {
        'url': url,
        'image': data_uri(url),
        'reference': reference_for(request_id),
    }
