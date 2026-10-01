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

# The storage handle is built once per process and shared across requests and
# threads. Both backends talk to an object store over HTTPS rather than holding
# a socket open, so there is nothing here to serialise against.
_handle = None
_bucket = None


def storage_bucket():
    """Return the ``(client, bucket)`` pair proofs are written to.

    Cloudflare R2 is the default. R2 speaks the S3 API, so boto3 talks to it
    directly against ``<account>.r2.cloudflarestorage.com`` -- no proxy, no
    second service to keep running, and a bucket that stays private unless it is
    deliberately published. STORAGE_BACKEND picks the backend; the Supabase path
    is still here for a project still holding proofs there, and both are
    configured entirely from the environment so no credential is ever written
    into a template.
    """
    global _handle, _bucket
    if _handle is None:
        backend = current_app.config['STORAGE_BACKEND']
        if backend == 'supabase':
            _handle, _bucket = _supabase_handle()
        else:
            _handle, _bucket = _r2_handle()
    return _handle, _bucket


def _r2_handle():
    """Build the S3 client for Cloudflare R2.

    The credentials are an R2 API token, not an AWS one, and they carry full
    bucket access, so they are treated as a server-side secret exactly like the
    key they replace. boto3 is imported here rather than at module scope so a
    misconfigured deployment still boots and reports the fault as an upload
    error, instead of refusing to start.
    """
    account_id = current_app.config['R2_ACCOUNT_ID']
    access_key = current_app.config['R2_ACCESS_KEY_ID']
    secret_key = current_app.config['R2_SECRET_ACCESS_KEY']
    bucket = current_app.config['R2_BUCKET']
    if not all((account_id, access_key, secret_key, bucket)):
        raise UploadError('Proof storage is not configured on this server.')

    import boto3
    from botocore.config import Config as S3Config

    timeout = current_app.config['STORAGE_TIMEOUT_SECONDS']
    client = boto3.client(
        's3',
        endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        # R2 ignores the region but SigV4 requires one, and "auto" is what
        # Cloudflare documents for it. Region-specific endpoints would 404.
        region_name='auto',
        config=S3Config(
            signature_version='s3v4',
            # A hung upload must not hold a request thread: the same reasoning as
            # DB_CONNECT_TIMEOUT, for the same reason.
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={'max_attempts': 3, 'mode': 'standard'},
        ),
    )
    return client, bucket


def _supabase_handle():
    """Build the Supabase Storage handle, for deployments still using it."""
    from supabase import ClientOptions, create_client

    url = current_app.config['SUPABASE_URL']
    key = current_app.config['SUPABASE_SECRET_KEY']
    if not url or not key:
        raise UploadError('Proof storage is not configured on this server.')
    bucket = current_app.config['SUPABASE_STORAGE_BUCKET']
    client = create_client(
        url,
        key,
        options=ClientOptions(
            storage={'timeout': current_app.config['STORAGE_TIMEOUT_SECONDS']}
        ),
    )
    return client.storage.from_(bucket), bucket


def _error_code(exc) -> str:
    """The S3 error code on a client exception, or '' for anything else."""
    response = getattr(exc, 'response', None)
    if isinstance(response, dict):
        return str(response.get('Error', {}).get('Code', ''))
    return ''


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
    proof is unrecoverable, so the key is checked before the write. S3's
    put_object overwrites silently, which makes that check the only thing
    standing between a re-run of the request and a destroyed document.
    """
    client, bucket = storage_bucket()

    if current_app.config['STORAGE_BACKEND'] == 'supabase':
        result = client.upload(key, payload, {
            'content-type': content_type,
            'upsert': 'false',
        })
        if getattr(result, 'error', None):
            current_app.logger.error('Proof upload rejected: %s', result.error)
            raise UploadError('The proof document could not be stored.')
        return

    try:
        client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _error_code(exc) not in ('404', 'NoSuchKey', 'NotFound'):
            raise
    else:
        raise UploadError('The proof document could not be stored.')

    client.put_object(
        Bucket=bucket, Key=key, Body=payload, ContentType=content_type,
    )


def fetch_proof(file_path: str) -> bytes:
    """Read a stored proof back for an authorised download."""
    key = normalise_key(file_path)
    try:
        client, bucket = storage_bucket()
        if current_app.config['STORAGE_BACKEND'] == 'supabase':
            return client.download(key)
        response = client.get_object(Bucket=bucket, Key=key)
        return response['Body'].read()
    except Exception as exc:
        current_app.logger.warning('Proof download failed for %s: %s', key, exc)
        raise UploadError('That document is no longer available.') from exc


def delete_proof(file_path: str) -> bool:
    key = normalise_key(file_path)
    try:
        client, bucket = storage_bucket()
        if current_app.config['STORAGE_BACKEND'] == 'supabase':
            client.remove([key])
        else:
            client.delete_object(Bucket=bucket, Key=key)
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