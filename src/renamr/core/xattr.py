"""Extended-attribute access for Linux and macOS via the platform ``libc``.

CPython only exposes ``os.*xattr`` on Linux, so both platforms are served through
:mod:`ctypes` with accurate per-platform signatures. Attributes are read or the
operation raises ``OSError``; callers fail closed rather than approximating
metadata with mode bits.
"""

from __future__ import annotations

import ctypes
import errno
import sys
from pathlib import Path

_IS_DARWIN = sys.platform == "darwin"
_LIBC: ctypes.CDLL | None = None


def supported() -> bool:
    """Return whether this runtime can read and write extended attributes."""
    return sys.platform in ("darwin", "linux")


def _libc() -> ctypes.CDLL:
    """Return the cached libc handle configured for this platform."""
    global _LIBC
    if _LIBC is not None:
        return _LIBC
    library = ctypes.CDLL(None, use_errno=True)
    if _IS_DARWIN:
        _configure_darwin(library)
    else:
        _configure_linux(library)
    _LIBC = library
    return library


def _configure_darwin(library: ctypes.CDLL) -> None:
    """Set the macOS xattr signatures (extra position/options arguments)."""
    library.listxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_int,
    ]
    library.listxattr.restype = ctypes.c_ssize_t
    library.getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    library.getxattr.restype = ctypes.c_ssize_t
    library.setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    library.setxattr.restype = ctypes.c_int


def _configure_linux(library: ctypes.CDLL) -> None:
    """Set the Linux xattr signatures."""
    library.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    library.listxattr.restype = ctypes.c_ssize_t
    library.getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    library.getxattr.restype = ctypes.c_ssize_t
    library.setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
    ]
    library.setxattr.restype = ctypes.c_int


def list_names(path: Path) -> list[str]:
    """Return the extended attribute names of a file.

    Raises:
        OSError: With the platform errno when unsupported or on failure.
    """
    if not supported():
        raise OSError(errno.ENOTSUP, "extended attributes unsupported")
    library = _libc()
    encoded = str(path).encode("utf-8")
    size = _list_size(library, encoded)
    if size < 0:
        raise _failure("listxattr")
    if size == 0:
        return []
    buffer = ctypes.create_string_buffer(size)
    written = _list_into(library, encoded, buffer, size)
    if written < 0:
        raise _failure("listxattr")
    return [
        name.decode("utf-8", errors="surrogateescape")
        for name in buffer.raw[:written].split(b"\x00")
        if name
    ]


def read(path: Path, name: str) -> bytes:
    """Return the value of one extended attribute.

    Raises:
        OSError: With the platform errno when unsupported or on failure.
    """
    if not supported():
        raise OSError(errno.ENOTSUP, "extended attributes unsupported")
    library = _libc()
    encoded = str(path).encode("utf-8")
    encoded_name = name.encode("utf-8")
    size = _read_size(library, encoded, encoded_name)
    if size < 0:
        raise _failure("getxattr")
    buffer = ctypes.create_string_buffer(size)
    written = _read_into(library, encoded, encoded_name, buffer, size)
    if written < 0:
        raise _failure("getxattr")
    return buffer.raw[:written]


def write(path: Path, name: str, value: bytes) -> None:
    """Set one extended attribute.

    Raises:
        OSError: With the platform errno when unsupported or on failure.
    """
    if not supported():
        raise OSError(errno.ENOTSUP, "extended attributes unsupported")
    library = _libc()
    encoded = str(path).encode("utf-8")
    encoded_name = name.encode("utf-8")
    buffer = ctypes.create_string_buffer(value, len(value))
    result = _write_value(library, encoded, encoded_name, buffer, len(value))
    if result != 0:
        raise _failure("setxattr")


def _list_size(library: ctypes.CDLL, encoded: bytes) -> int:
    """Query the buffer size for ``listxattr``."""
    if _IS_DARWIN:
        return int(library.listxattr(encoded, None, 0, 0))
    return int(library.listxattr(encoded, None, 0))


def _list_into(
    library: ctypes.CDLL, encoded: bytes, buffer: ctypes.Array[ctypes.c_char], size: int
) -> int:
    """Fill a buffer with attribute names, returning the byte count."""
    if _IS_DARWIN:
        return int(library.listxattr(encoded, buffer, size, 0))
    return int(library.listxattr(encoded, buffer, size))


def _read_size(library: ctypes.CDLL, encoded: bytes, encoded_name: bytes) -> int:
    """Query the buffer size for ``getxattr``."""
    if _IS_DARWIN:
        return int(library.getxattr(encoded, encoded_name, None, 0, 0, 0))
    return int(library.getxattr(encoded, encoded_name, None, 0))


def _read_into(
    library: ctypes.CDLL,
    encoded: bytes,
    encoded_name: bytes,
    buffer: ctypes.Array[ctypes.c_char],
    size: int,
) -> int:
    """Read an attribute value into a buffer, returning the byte count."""
    if _IS_DARWIN:
        return int(library.getxattr(encoded, encoded_name, buffer, size, 0, 0))
    return int(library.getxattr(encoded, encoded_name, buffer, size))


def _write_value(
    library: ctypes.CDLL,
    encoded: bytes,
    encoded_name: bytes,
    buffer: ctypes.Array[ctypes.c_char],
    size: int,
) -> int:
    """Set an attribute value, returning the raw result code."""
    if _IS_DARWIN:
        return int(library.setxattr(encoded, encoded_name, buffer, size, 0, 0))
    return int(library.setxattr(encoded, encoded_name, buffer, size, 0))


def _failure(operation: str) -> OSError:
    """Build an OSError from the current ctypes errno."""
    return OSError(ctypes.get_errno(), f"{operation} failed")
