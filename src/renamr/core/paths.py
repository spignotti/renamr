"""Private state paths, bootstrap selection, and owner-only filesystem helpers.

The state directory holds the catalog, writer lock, backups, and sanitized logs.
Directories this module creates are ``0700``; files it creates are ``0600``.
Pre-existing directories and files are never rewritten: their ownership and mode
are validated and a mismatch fails closed. Critical opens refuse to follow
symlinks. Bootstrap selection is explicit: an ``--config`` path, then
``$XDG_CONFIG_HOME/renamr/config.toml``, then ``~/.config/renamr/config.toml``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from renamr.core.errors import ConfigError, StateError

BOOTSTRAP_FORMAT_VERSION = 2
CATALOG_FILENAME = "catalog.sqlite3"
LOCK_FILENAME = "writer.lock"
BACKUPS_DIRNAME = "backups"
LOGS_DIRNAME = "logs"
EXPORTS_DIRNAME = "exports"

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_FORBIDDEN_BITS = 0o077

_CLOUD_SYNC_MARKERS = (
    ("Library/Mobile Documents", "iCloud Drive"),
    ("Dropbox", "Dropbox"),
    ("Google Drive", "Google Drive"),
    ("GoogleDrive", "Google Drive"),
    ("OneDrive", "OneDrive"),
    ("Library/CloudStorage", "a cloud storage provider"),
)


class BootstrapConfig(BaseModel):
    """Minimal v2 bootstrap: format version plus the private state directory."""

    model_config = ConfigDict(extra="forbid")

    format_version: int = Field(default=BOOTSTRAP_FORMAT_VERSION)
    state_dir: Path


def default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the platform default private state directory."""
    environment = os.environ if env is None else env
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "renamr"
    xdg_state = environment.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / "renamr"
    return Path.home() / ".local" / "state" / "renamr"


def config_candidates(
    explicit: Path | None = None, env: Mapping[str, str] | None = None
) -> list[Path]:
    """Return bootstrap config candidates in descending precedence."""
    if explicit is not None:
        return [Path(explicit).expanduser()]
    environment = os.environ if env is None else env
    xdg_config = environment.get("XDG_CONFIG_HOME")
    if xdg_config:
        return [Path(xdg_config) / "renamr" / "config.toml"]
    return [Path.home() / ".config" / "renamr" / "config.toml"]


def resolve_bootstrap_config(
    explicit: Path | None = None, env: Mapping[str, str] | None = None
) -> Path | None:
    """Return the first existing bootstrap config path, or None."""
    for candidate in config_candidates(explicit, env):
        if candidate.is_file():
            return candidate
    return None


def derive_state_dir(explicit: Path | None, env: Mapping[str, str] | None = None) -> Path:
    """Resolve an explicit state directory or fall back to the platform default."""
    if explicit is not None:
        return Path(explicit).expanduser()
    return default_state_dir(env)


def load_bootstrap(path: Path) -> BootstrapConfig:
    """Load and validate a bootstrap TOML file.

    Raises:
        ConfigError: If the file is missing, unreadable, or malformed.
    """
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"bootstrap config is unreadable: {exc.strerror}") from None
    except tomllib.TOMLDecodeError:
        raise ConfigError("bootstrap config is not valid TOML") from None
    try:
        config = BootstrapConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"bootstrap config is invalid: {exc}") from None
    if config.format_version != BOOTSTRAP_FORMAT_VERSION:
        raise ConfigError(f"unsupported bootstrap format version {config.format_version}")
    return config


def write_bootstrap(path: Path, state_dir: Path) -> Path:
    """Write a v2 bootstrap file pointing at ``state_dir``.

    The state path is emitted as an escaped TOML basic string so quotes,
    backslashes, and spaces round-trip. The config directory is not forced
    private; the file itself is created ``0600``.
    """
    content = (
        f"format_version = {BOOTSTRAP_FORMAT_VERSION}\n"
        f"state_dir = {json.dumps(str(Path(state_dir)))}\n"
    )
    return write_private_file(Path(path), content.encode("utf-8"), require_private_parent=False)


def ensure_private_dir(path: Path) -> Path:
    """Return a directory that exists and is owner-only.

    A directory created by this call is ``0700``. A pre-existing directory is
    validated (real directory, owned by the current user, no group/other access)
    and never rewritten; a mismatch raises.

    Raises:
        StateError: If the path is a symlink, not a directory, not owned by the
            current user, or has group/other permissions.
    """
    directory = Path(path)
    if directory.is_symlink():
        raise StateError("state path must not be a symlink")
    if directory.exists():
        _validate_private_dir(directory)
        return directory
    try:
        directory.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        os.chmod(directory, PRIVATE_DIR_MODE)
    except OSError:
        raise StateError("state directory could not be created privately") from None
    _validate_private_dir(directory)
    return directory


def ensure_private_file(path: Path) -> Path:
    """Return a regular file that exists and is owner-only.

    A file created by this call is ``0600``. A pre-existing file is validated and
    never rewritten.

    Raises:
        StateError: If the path is a symlink, not a regular file, not owned by
            the current user, or has group/other permissions.
    """
    file_path = Path(path)
    if file_path.is_symlink():
        raise StateError("state file must not be a symlink")
    if file_path.exists():
        _validate_private_file(file_path)
        return file_path
    directory = ensure_private_dir(file_path.parent)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _nofollow_flag()
    try:
        descriptor = os.open(directory / file_path.name, flags, PRIVATE_FILE_MODE)
    except OSError:
        raise StateError("state file could not be created privately") from None
    os.close(descriptor)
    _validate_private_file(file_path)
    return file_path


def write_private_file(
    path: Path,
    data: bytes,
    *,
    require_private_parent: bool = True,
    overwrite: bool = True,
) -> Path:
    """Atomically write bytes to an owner-only file.

    Uses a unique exclusive temp file so a pre-planted temp name or symlink
    cannot redirect the write. When ``require_private_parent`` is true the parent
    directory must be private (state layout); otherwise the parent is only
    required to exist, so explicit exports and bootstrap files can live in an
    ordinary user directory while the file itself stays ``0600``.

    With ``overwrite=False`` the file is installed by an atomic hard link, so an
    occupied destination is refused and its contents and permissions are left
    untouched even if it appears during the write.

    Raises:
        StateError: If a private parent is required but not private, or the atomic
            install fails or is refused.
    """
    destination = Path(path)
    if require_private_parent:
        directory = ensure_private_dir(destination.parent)
    else:
        directory = Path(destination.parent).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_symlink():
        raise StateError("state file must not be a symlink")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=directory)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temp_path, destination)
        else:
            _link_no_clobber(temp_path, destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    _validate_private_file(destination)
    return destination


def _link_no_clobber(source: Path, destination: Path) -> None:
    """Install a file without replacing an existing destination."""
    try:
        os.link(source, destination)
    except FileExistsError:
        raise StateError(
            "destination already exists; overwrite requires explicit confirmation"
        ) from None
    except OSError:
        raise StateError("file could not be installed atomically") from None
    finally:
        source.unlink(missing_ok=True)


def harden_sidecars(db_path: Path) -> None:
    """Best-effort privacy for SQLite ``-wal``/``-shm`` sidecars.

    The sidecars are created by SQLite with the process umask; tighten them when
    they are not symlinks and are owned by us.
    """
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.is_symlink() or not sidecar.exists():
            continue
        try:
            if sidecar.stat().st_uid == os.getuid():
                os.chmod(sidecar, PRIVATE_FILE_MODE)
        except OSError:
            continue


def reject_symlink(path: Path, *, what: str) -> Path:
    """Return ``path`` unchanged, refusing a symlink.

    Raises:
        StateError: If the path is a symlink.
    """
    candidate = Path(path)
    if candidate.is_symlink():
        raise StateError(f"{what} must not be a symlink")
    return candidate


def is_within(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is ``parent`` or a descendant of it."""
    try:
        resolved_path = Path(path).resolve()
        resolved_parent = Path(parent).resolve()
    except OSError:
        return False
    return resolved_path == resolved_parent or resolved_parent in resolved_path.parents


def state_overlaps_root(state_dir: Path, root: Path) -> bool:
    """Return whether the state directory and a managed root overlap."""
    return is_within(state_dir, root) or is_within(root, state_dir)


def cloud_sync_hint(path: Path) -> str | None:
    """Return a provider name if ``path`` is inside a known cloud-sync location."""
    text = str(Path(path))
    for marker, provider in _CLOUD_SYNC_MARKERS:
        if marker in text:
            return provider
    return None


def require_outside_roots(state_dir: Path, roots: list[Path]) -> None:
    """Raise if the state directory overlaps any managed root."""
    for root in roots:
        if state_overlaps_root(state_dir, root):
            raise StateError("state directory must not be inside a managed root")


def _validate_private_dir(path: Path) -> None:
    """Raise unless a directory is a real, current-user, owner-only dir."""
    if path.is_symlink() or not path.is_dir():
        raise StateError("state directory is not a real directory")
    info = path.stat()
    if info.st_uid != os.getuid():
        raise StateError("state directory is not owned by the current user")
    if info.st_mode & _FORBIDDEN_BITS:
        raise StateError("state directory permissions allow group or other access")


def _validate_private_file(path: Path) -> None:
    """Raise unless a file is a real, current-user, owner-only regular file."""
    if path.is_symlink() or not path.is_file():
        raise StateError("state file is not a regular file")
    info = path.stat()
    if info.st_uid != os.getuid():
        raise StateError("state file is not owned by the current user")
    if info.st_mode & _FORBIDDEN_BITS:
        raise StateError("state file permissions allow group or other access")


def _nofollow_flag() -> int:
    """Return ``O_NOFOLLOW`` when the platform provides it."""
    return getattr(os, "O_NOFOLLOW", 0)
