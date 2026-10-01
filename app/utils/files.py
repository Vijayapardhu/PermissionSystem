import os

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
    """Raised when an uploaded proof fails validation."""


MAX_PROOF_BYTES = 5 * 1024 * 1024


def validate_and_store(file_storage) -> dict:
    """Validate an uploaded proof and write it under year/month buckets.

    Returns the stored file's metadata; the caller records the path in MySQL.
    The original filename is preserved only as a database column value, never
    as a filesystem path.
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

    year, month, _ = _buckets()
    directory = os.path.join(current_app.config['UPLOAD_FOLDER'], year, month)
    os.makedirs(directory, exist_ok=True)

    stored = generate_stored_filename(original)
    destination = os.path.join(directory, stored)

    file_storage.save(destination)

    size = os.path.getsize(destination)
    if size == 0:
        os.remove(destination)
        raise UploadError('The uploaded file is empty.')
    if size > MAX_PROOF_BYTES:
        os.remove(destination)
        raise UploadError('The proof document exceeds the 5 MB limit.')

    return {
        'original_filename': original,
        'stored_filename': stored,
        'file_path': os.path.join(year, month, stored).replace('\\', '/'),
        'file_type': detected,
        'file_size': size,
    }


def resolve_on_disk(file_path: str) -> str:
    """Map a stored relative path to an absolute path, refusing escapes."""
    root = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    absolute = os.path.abspath(os.path.join(root, file_path))
    if not absolute.startswith(root + os.sep):
        raise UploadError('Invalid proof path.')
    return absolute


def delete_proof(file_path: str) -> bool:
    try:
        absolute = resolve_on_disk(file_path)
    except UploadError:
        return False
    if os.path.isfile(absolute):
        os.remove(absolute)
        return True
    return False


def _buckets():
    from datetime import datetime
    now = datetime.now()
    return str(now.year), f'{now.month:02d}', ''


__all__ = [
    'UploadError',
    'validate_and_store',
    'resolve_on_disk',
    'delete_proof',
    'is_university_email',
    'secure_filename',
]