"""Content hashing and filesystem fingerprints.

Content identity is always the full SHA-256 digest of the bytes; a partial or
truncated digest is never used as an identity. Callers that must bound work check
the size first and record a durable limit state instead of reading a huge file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Fingerprint:
    """Observed filesystem identity and full content hash for one file."""

    content_hash: str
    size_bytes: int
    mtime_ns: int | None
    dev: int | None
    ino: int | None


def hash_file(path: Path) -> str:
    """Return the full SHA-256 hex digest of a file, streamed in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(path: Path) -> Fingerprint:
    """Return a fingerprint (full hash, size, timestamps, fs identity) for a file."""
    stat = path.stat()
    return Fingerprint(
        content_hash=hash_file(path),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        dev=stat.st_dev,
        ino=stat.st_ino,
    )


def stat_matches(
    path: Path, *, dev: int | None, ino: int | None, size_bytes: int, mtime_ns: int | None
) -> bool:
    """Return whether a file's stat still matches a stored fingerprint.

    Used as a fast path so an unchanged file needs no re-hash; a missing record or
    any changed field returns False and forces a real hash.

    Raises:
        OSError: If the file cannot be stat'ed.
    """
    info = path.stat()
    if dev is not None and info.st_dev != dev:
        return False
    if ino is not None and info.st_ino != ino:
        return False
    if info.st_size != size_bytes:
        return False
    if mtime_ns is not None and info.st_mtime_ns != mtime_ns:
        return False
    return True


def exceeds_limit(size_bytes: int, max_bytes: int) -> bool:
    """Return whether a file size exceeds a configured byte limit."""
    return size_bytes > max_bytes
