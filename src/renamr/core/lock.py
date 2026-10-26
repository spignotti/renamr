"""Cross-process exclusive writer lock on a persistent lock inode.

The lock is an ``fcntl.flock`` advisory lock on a persistent file in the state
directory. The file is never unlinked or recreated while the lock may be held, so
a crash releases the lock through the kernel closing the descriptor rather than
through stale-PID guessing. Every writer workflow acquires this same lock.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path

from renamr.core.errors import LockError
from renamr.core.paths import LOCK_FILENAME, ensure_private_dir, ensure_private_file

_POLL_SECONDS = 0.05


class WriterLock:
    """One live exclusive lock descriptor on the state lock file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    @classmethod
    def for_state(cls, state_dir: Path) -> WriterLock:
        """Return the writer lock for a state directory."""
        return cls(Path(state_dir) / LOCK_FILENAME)

    @property
    def path(self) -> Path:
        """Return the lock file path."""
        return self._path

    @property
    def held(self) -> bool:
        """Return whether this instance currently holds the lock."""
        return self._fd is not None

    def acquire(self, *, blocking: bool = True, timeout: float | None = None) -> bool:
        """Try to acquire the exclusive lock.

        Args:
            blocking: Wait for the lock instead of returning immediately.
            timeout: Optional seconds to wait before giving up.

        Returns:
            True when the lock is now held, False when a non-blocking attempt
            failed.

        Raises:
            LockError: If the lock file cannot be opened safely or a blocking wait
                times out.
        """
        if self._fd is not None:
            return True
        ensure_private_dir(self._path.parent)
        ensure_private_file(self._path)
        if self._path.is_symlink():
            raise LockError("writer lock must not be a symlink")
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except OSError:
            raise LockError("could not open the writer lock file") from None
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                if _try_lock(descriptor):
                    self._fd = descriptor
                    return True
                if not blocking:
                    os.close(descriptor)
                    return False
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise LockError("timed out waiting for the writer lock")
                time.sleep(_POLL_SECONDS)
        except OSError:
            os.close(descriptor)
            raise LockError("could not open the writer lock file") from None

    def release(self) -> None:
        """Release the lock and close the descriptor; safe to call twice."""
        if self._fd is None:
            return
        descriptor, self._fd = self._fd, None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> WriterLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _try_lock(descriptor: int) -> bool:
    """Attempt a non-blocking exclusive flock."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise
    return True
