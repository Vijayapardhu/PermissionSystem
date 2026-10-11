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


#: Faculty leave uses its own prefix. The signature covers the whole reference
#: string, so an LVR- link can never be replayed as a REQ- one and reach the
#: permission register -- the two point at different records with different
#: contents, and one of them is a medical reason.
LEAVE_PREFIX = 'LVR-'


def reference_for_leave(leave_id: int) -> str:
    """The reference a leave letter carries, e.g. 7 -> 'LVR-0007'."""
    return f'{LEAVE_PREFIX}{int(leave_id):04d}'


def public_base_url() -> str:
    """The origin verification links are minted against.

    A printed link has to be openable by whoever is holding the paper, which is
    rarely the machine that printed it. So the origin is resolved in this order:

    1. ``PUBLIC_BASE_URL``, if set. An explicit answer beats an inferred one.
    2. The host the request arrived on, when the configured origin is loopback.
       ``REDIRECT_URI`` has to be ``http://localhost:5000/auth/callback``
       locally because that is what Entra registered, and reusing that value for
       printed links minted every QR at localhost -- so a letter printed from a
       laptop and scanned by a phone led nowhere. When the configured origin is
       loopback it is a local development default rather than a published
       address, and the reader is demonstrably somewhere else.
    3. ``REDIRECT_URI``. This is what keeps production correct: Render terminates
       TLS in front of gunicorn and nothing in this stack rewrites
       ``wsgi.url_scheme``, so ``url_for(_external=True)`` emits ``http://``
       here, and an http link printed on a formal letter is wrong in a way a user
       cannot fix. The configured origin is the registered one and carries the
       right scheme.
    4. The forwarded headers, for deployments that never set either.

    The step-2 exemption is deliberately limited to loopback. A configured
    *public* origin is never overridden, so production cannot be talked into
    emitting links to whatever Host header arrived.
    """
    configured = current_app.config.get('PUBLIC_BASE_URL')
    if configured:
        parts = urlsplit(configured)
        if parts.scheme in ('http', 'https') and parts.netloc:
            return f'{parts.scheme}://{parts.netloc}'

    forwarded = request.headers.get('X-Forwarded-Proto', '')
    scheme = forwarded.split(',')[0].strip().lower()
    if scheme not in ('http', 'https'):
        scheme = request.scheme
    host = request.headers.get('X-Forwarded-Host', '').split(',')[0].strip()
    served = f'{scheme}://{host or request.host}'.rstrip('/')

    redirect = current_app.config.get('REDIRECT_URI')
    if redirect:
        parts = urlsplit(redirect)
        if parts.scheme in ('http', 'https') and parts.netloc:
            configured_origin = f'{parts.scheme}://{parts.netloc}'
            if not _is_loopback(parts.hostname):
                return configured_origin
            # Loopback configured and a real host served: the reader is not on
            # this machine, so the machine's own address is the useless one.
            if not _is_loopback((urlsplit(served).hostname or '')):
                return served
            return configured_origin

    return served


def _is_loopback(hostname: str) -> bool:
    """Whether a host names this machine.

    ``localhost`` and the 127.0.0.0/8 range, plus the empty string -- an unset
    Host header is not evidence of a published address either.
    """
    if not hostname:
        return True
    host = hostname.strip().lower().strip('[]')
    if host in ('localhost', '::1', '0.0.0.0'):
        return True
    return host.startswith('127.')


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
    return _link(reference_for(request_id))


def leave_verification_url(leave_id: int) -> str:
    """The link on a faculty leave letter."""
    return _link(reference_for_leave(leave_id))


def _link(reference: str) -> str:
    """Sign a reference and mint the public URL that resolves it."""
    token = _serializer().dumps(reference)
    return (f'{public_base_url()}'
            f'{url_for("auth.verify_letter", reference=f"{reference}.{token}")}')


def _verified_reference(reference: str) -> str:
    """The reference a signed link names, or raise ValueError.

    Shared by both kinds of link. Failing here keeps the endpoint from
    distinguishing "no such record" from "forged signature", which would otherwise
    be a free oracle for testing which reference numbers exist.
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
    return claimed


def _id_from(reference: str, prefix: str) -> int:
    digits = reference.removeprefix(prefix)
    if not digits.isdigit():
        raise ValueError(f'reference is not a {prefix.rstrip("-")} number')
    record_id = int(digits)
    if record_id < 1:
        raise ValueError('reference is out of range')
    return record_id


def resolve_reference(reference: str) -> int:
    """Return the permission request id a verification link names."""
    return _id_from(_verified_reference(reference), 'REQ-')


def resolve_leave_reference(reference: str) -> int:
    """Return the faculty leave id a verification link names.

    An `LVR-` link raises here and a `REQ-` link raises in `resolve_reference`.
    The signature covers the prefix, so neither can be presented as the other.
    """
    return _id_from(_verified_reference(reference), LEAVE_PREFIX)


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


def leave_letter_qr(leave_id: int) -> dict:
    """The same, for a faculty leave letter."""
    url = leave_verification_url(leave_id)
    return {
        'url': url,
        'image': data_uri(url),
        'reference': reference_for_leave(leave_id),
    }
