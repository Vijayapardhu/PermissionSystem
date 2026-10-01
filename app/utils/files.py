import json
import mimetypes
import os
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

# The app is built once per process and the bucket handle it hands out is safe to
# share across requests and threads. firebase-admin's own client is thread-safe and
# refreshes its own access token, so there is nothing here to serialise against.
_app = None
_bucket = None


def storage_bucket():
    """Return the Firebase Storage bucket proofs are written to.

    The bucket is private and stays private: proofs are medical and identity
    documents, and nothing in this app hands out a public URL for one. They leave
    only through an authorised Flask route.

    Credentials come from the environment and never from the repository. Either
    FIREBASE_CREDENTIALS_JSON (the service account's JSON, inline, which is what a
    Render environment variable holds) or FIREBASE_CREDENTIALS_PATH (a file on
    disk, which is how it is usually set locally). firebase-admin is imported
    here rather than at module scope so a deployment without credentials still
    boots and reports the fault as an upload error instead of refusing to start.
    """
    global _app, _bucket
    if _app is None:
        import firebase_admin
        from firebase_admin import credentials, storage

        project_id = current_app.config['FIREBASE_PROJECT_ID']
        options = _credential_options()
        if not options:
            raise UploadError('Proof storage is not configured on this server.')

        # options= explicitly: initialize_app's first positional argument is the
        # credential, not the options, so passing the dict there makes it look
        # for a credential in a dict and refuse to start.
        _app = firebase_admin.initialize_app(options=options, name=_APP_NAME)
        _bucket = storage.bucket(
            current_app.config['FIREBASE_STORAGE_BUCKET'], app=_app
        )
    return _bucket


_APP_NAME = 'permission-system'


def _credential_options() -> dict:
    """Build firebase-admin's options, or {} when nothing is configured."""
    project_id = current_app.config['FIREBASE_PROJECT_ID']

    inline = (current_app.config['FIREBASE_CREDENTIALS_JSON'] or '').strip()
    if inline:
        from firebase_admin import credentials
        try:
            # Render's dashboard holds multi-line env vars, so the JSON arrives
            # with its newlines intact and has to be parsed, not pattern-matched.
            return {
                'credential': credentials.Certificate(json.loads(inline)),
                'projectId': project_id or None,
            }
        except (ValueError, TypeError):
            current_app.logger.error(
                'FIREBASE_CREDENTIALS_JSON is not valid JSON')
            raise UploadError('Proof storage is not configured on this server.')

    path = (current_app.config['FIREBASE_CREDENTIALS_PATH'] or '').strip()
    if path:
        if not os.path.exists(path):
            current_app.logger.error(
                'FIREBASE_CREDENTIALS_PATH does not exist: %s', path)
            raise UploadError('Proof storage is not configured on this server.')
        from firebase_admin import credentials
        return {
            'credential': credentials.Certificate(path),
            'projectId': project_id or None,
        }

    return {}


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

    Returns the stored object's metadata; the caller records the key in the
    database. The original filename is preserved only as a database column
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
        _put(key, payload,
             mimetypes.guess_type(stored)[0] or f'application/{detected}')
    except UploadError:
        raise
    except Exception as exc:
        current_app.logger.exception('Proof upload failed')
        raise UploadError('The proof document could not be stored.') from exc

    return {
        'original_filename': original,
        'stored_filename': stored,
        'file_path': key,
        'file_type': detected,
        'file_size': size,
    }


def _put(key: str, payload: bytes, content_type: str) -> None:
    """Write the object, refusing to overwrite one that is already there.

    Two UUIDs colliding is astronomically unlikely, and clobbering an existing
    proof is unrecoverable, so the key is checked before the write: the bucket
    would otherwise replace the object silently.
    """
    blob = storage_bucket().blob(key)

    if blob.exists():
        raise UploadError('The proof document could not be stored.')

    blob.upload_from_string(
        payload,
        content_type=content_type,
        # Never overwrite, whatever the check above found. The two together mean
        # a lost race cannot destroy a document that is already stored.
        if_generation_match=0,
    )


def fetch_proof(file_path: str) -> bytes:
    """Read a stored proof back for an authorised download."""
    key = normalise_key(file_path)
    try:
        return storage_bucket().blob(key).download_as_bytes()
    except Exception as exc:
        current_app.logger.warning('Proof download failed for %s: %s', key, exc)
        raise UploadError('That document is no longer available.') from exc


def delete_proof(file_path: str) -> bool:
    key = normalise_key(file_path)
    try:
        storage_bucket().blob(key).delete()
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
    'storage_bucket',
    'is_university_email',
    'secure_filename',
]