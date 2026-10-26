"""Filesystem security-metadata capability checks that fail closed.

Cross-volume publication must not silently drop metadata the standard library
cannot transfer. Access control lists are detected explicitly: when they exist,
or when detection is unavailable, publication blocks with the source intact
rather than approximating access controls with mode bits.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from renamr.core import xattr

_IS_DARWIN = sys.platform == "darwin"
_ACL_TYPE_EXTENDED = 0x00000100
_ACL_FIRST_ENTRY = 0
_ACLLIB: ctypes.CDLL | None = None


def detection_available() -> bool:
    """Return whether this runtime can detect an access control list."""
    if _IS_DARWIN:
        return getattr(_acllib(), "acl_get_file", None) is not None
    return xattr.supported()


def acl_status(path: Path) -> bool | None:
    """Return whether a file has an ACL, or None when that cannot be determined."""
    if _IS_DARWIN:
        return _darwin_acl_status(path)
    return _linux_acl_status(path)


def _linux_acl_status(path: Path) -> bool | None:
    """Detect a POSIX ACL through the ``system.posix_acl_*`` xattrs."""
    if not xattr.supported():
        return None
    try:
        names = set(xattr.list_names(path))
    except OSError:
        return None
    return "system.posix_acl_access" in names or "system.posix_acl_default" in names


def _darwin_acl_status(path: Path) -> bool | None:
    """Detect a macOS extended ACL using ``acl_get_file``."""
    library = _acllib()
    get_file = getattr(library, "acl_get_file", None)
    if get_file is None:
        return None
    acl = get_file(str(path).encode("utf-8"), _ACL_TYPE_EXTENDED)
    if not acl:
        return False
    try:
        entry = ctypes.c_void_p()
        return int(library.acl_get_entry(acl, _ACL_FIRST_ENTRY, ctypes.byref(entry))) == 0
    finally:
        library.acl_free(acl)


def _acllib() -> ctypes.CDLL:
    """Return the cached libc handle configured for ACL calls when available."""
    global _ACLLIB
    if _ACLLIB is not None:
        return _ACLLIB
    library = ctypes.CDLL(None, use_errno=True)
    try:
        _configure_acl(library)
    except AttributeError:
        pass
    _ACLLIB = library
    return library


def _configure_acl(library: ctypes.CDLL) -> None:
    """Set the macOS ACL function signatures from libSystem."""
    library.acl_get_file.restype = ctypes.c_void_p
    library.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.acl_get_entry.restype = ctypes.c_int
    library.acl_get_entry.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    library.acl_free.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
