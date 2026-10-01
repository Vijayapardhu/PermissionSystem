import mimetypes
import posixpath

from datetime import datetime

from flask import current_app
from werkzeug.utils import secure_filename

from app.utils.security import (
    allowed_file,
    file_extension,
    generate_stored_filename,
    is_university_email,
    sanitize_filename,
    sniff_extension,
)


class UploadError(Exception):
    """Raised when an uploaded proof fails validation or storage."""


MAX_PROOF_BYTES = 5 * 1024 * 1024

_client = None


def storage_client():
    """Return the shared Supabase client, creating it on first use.

    supabase-py talks to Storage over HTTPS rather than holding a database
    socket, so one module-level client is safe to share across requests and
    threads.
    """
    global _client
    if _client is None:
        from supabase import ClientOptions, create_client

        url = current_app.config['SUPABASE_URL']
        key = current_app.config['SUPABASE_SECRET_KEY']
        if not url or not key:
            raise UploadError(
                'Proof storage is not configured on this server.'
            )
        _client = create_client(
            url,
            key,
            options=ClientOptions(
                storage={
                    'timeout': current_app.config['STORAGE_TIMEOUT_SECONDS'],
                }
            ),
        )
    return _client


def _bucket():
    return current_app.config['SUPABASE_STORAGE_BUCKET']


def normalise_key(file_path: str) -> str:
    """Validate a stored object key, refusing anything outside the bucket root.

    Keys are bucket-relative and POSIX-style. An absolute path, a drive letter,
    a ``..`` segment or an empty segment is refused rather than normalised, so a
    traversal attempt fails loudly instead of silently resolving elsewhere.
    """
    if not file_path or not isinstance(file_path, str):
        raise UploadError('Invalid proof path.')

    key = file_path.strip().replace('\\', '/')

    if key.startswith('/') or '://' in key or (len(key) > 1 and key[1] == ':'):
        raise UploadError('Invalid proof path.')

    parts = key.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise UploadError('Invalid proof path.')

    # Belt and braces: confirm the cleaned key stays inside the root.
    if posixpath.normpath(key) != key or key.startswith('..'):
        raise UploadError('Invalid proof path.')

    return key


def validate_and_store(file_storage) -> dict:
    """Validate an uploaded proof and write it under year/month keys.

    Returns the stored object's metadata; the caller records the key in
    Postgres. The original filename is preserved only as a database column
    value, never as part of an object key.
    """
    if file_storage is None or not file_storage.filename:
        raise UploadError('A proof document is required.')

    original = sanitize_filename(file_storage.filename)

    if not allowed_file(original):
        raise UploadError(
            'Only PDF, JPG, JPEG and PNG proof documents are accepted.'
        )

    file_storage.stream.seek(0)
    head = file_storage.stream.read(16)
    file_storage.stream.seek(0)

    if not head:
        raise UploadError('The uploaded file is empty.')

    detected = sniff_extension(head)
    if not detected:
        raise UploadError('The uploaded file is not a valid PDF or image.')

    if detected != file_extension(original):
        raise UploadError(
            f'The file content ({detected.upper()}) does not match its '
            f'extension ({file_extension(original).upper()}).'
        )

    payload = file_storage.stream.read()
    file_storage.stream.seek(0)

    if not payload:
        raise UploadError('The uploaded file is empty.')

    size = len(payload)
    if size > MAX_PROOF_BYTES:
        raise UploadError('The proof document exceeds the 5 MB limit.')

    year, month, _ = _buckets()
    stored = generate_stored_filename(original)
    key = f'{year}/{month}/{stored}'

    try:
        result = storage_client().storage.from_(_bucket()).upload(
            key,
            payload,
            {
                'content-type': mimetypes.guess_type(stored)[0]
                                or f'application/{detected}',
                # Never overwrite: two UUIDs colliding is astronomically
                # unlikely, and clobbering an existing proof is unrecoverable.
                'upsert': 'false',
            },
        )
    except UploadError:
        raise
    except Exception as exc:
        current_app.logger.exception('Proof upload failed')
        raise UploadError('The proof document could not be stored.') from exc

    if getattr(result, 'error', None):
        current_app.logger.error('Proof upload rejected: %s', result.error)
        raise UploadError('The proof document could not be stored.')

    return {
        'original_filename': original,
        'stored_filename': stored,
        'file_path': key,
        'file_type': detected,
        'file_size': size,
    }


def fetch_proof(file_path: str) -> bytes:
    """Read a stored proof back for an authorised download."""
    key = normalise_key(file_path)
    try:
        return storage_client().storage.from_(_bucket()).download(key)
    except Exception as exc:
        current_app.logger.warning('Proof download failed for %s: %s', key, exc)
        raise UploadError('That document is no longer available.') from exc


def delete_proof(file_path: str) -> bool:
    key = normalise_key(file_path)
    try:
        storage_client().storage.from_(_bucket()).remove([key])
        return True
    except Exception:
        current_app.logger.exception('Proof delete failed for %s', key)
        return False


def _buckets():
    now = datetime.now()
    return str(now.year), f'{now.month:02d}', ''


__all__ = [
    'UploadError',
    'validate_and_store',
    'normalise_key',
    'fetch_proof',
    'delete_proof',
    'is_university_email',
    'secure_filename',
]
