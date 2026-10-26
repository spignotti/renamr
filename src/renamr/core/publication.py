"""Journaled file publication and recovery for one operation item.

Publication never overwrites: a same-volume move links the source into place and
only then retires the source, and a cross-volume move stages verified bytes in
the destination directory before linking them into place. Target identity
(device, inode) is journaled at publish time, and the staging file is kept until
the publication is recorded, so recovery never adopts a foreign file that merely
shares the bytes. Source identity is rechecked before retirement, security
metadata that cannot be preserved blocks the move, and stray filesystem errors
map to journaled outcomes rather than escaping.
"""

from __future__ import annotations

import errno
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from renamr.core import catalog, fsmeta, roots, xattr
from renamr.core.catalog import Presence
from renamr.core.db import Database
from renamr.core.errors import (
    DestinationOccupiedError,
    JournalError,
    MetadataUnsupportedError,
    OperationError,
    PreconditionError,
)
from renamr.core.facts import record_review
from renamr.core.hashing import hash_file
from renamr.core.journal import STAGE_PREFIX, FaultInjector, ItemState
from renamr.core.runitem import RunGuard, RunItem, row_to_item

_COPY_CHUNK = 1024 * 1024


def apply_item(
    db: Database, item: RunItem, fault: FaultInjector | None, guard: RunGuard | None
) -> None:
    """Advance one item through the journal, blocking rather than overwriting."""
    try:
        if guard is not None:
            guard(item)
        _advance(db, item, fault)
    except (DestinationOccupiedError, PreconditionError, MetadataUnsupportedError) as exc:
        _block(db, item, exc.category)
    except OperationError as exc:
        _fail(db, item, exc.category)
    except OSError:
        _fail(db, item, "filesystem_error")


def recover_item(db: Database, item: RunItem, guard: RunGuard | None) -> str:
    """Reconcile one non-terminal item, preserving bytes and ownership.

    Returns:
        One of ``resumed``, ``discarded``, ``blocked``, or ``untouched``.
    """
    if guard is not None:
        try:
            guard(item)
        except OperationError as exc:
            _block(db, item, exc.category)
            return "blocked"
    stage = _stage_path(item)
    if item.target_path is not None and item.state in _OWNED_STATES:
        if _correlate_target(db, item) and _resume(db, item, stage):
            return "resumed"
        if item.target_path.exists():
            _block(db, item, "target_not_correlated")
            return "blocked"
    if stage.exists():
        if item.source_path.exists() and _source_unchanged(item):
            stage.unlink(missing_ok=True)
            _set_state(db, item.item_id, ItemState.PLANNED)
            return "discarded"
        _block(db, item, "staging_holds_only_copy")
        return "blocked"
    if item.source_path.exists():
        return "untouched"
    _block(db, item, "source_missing_during_recovery")
    return "blocked"


_OWNED_STATES = (ItemState.STAGED, ItemState.PUBLISHED, ItemState.SOURCE_RETIRED)


def _resume(db: Database, item: RunItem, stage: Path) -> bool:
    """Complete a verified publication, retiring the source if it is unchanged."""
    if item.source_path.exists():
        if not _source_unchanged(item):
            stage.unlink(missing_ok=True)
            _block(db, item, "source_changed_during_recovery")
            return False
        item.source_path.unlink()
        _set_state(db, item.item_id, ItemState.SOURCE_RETIRED)
    stage.unlink(missing_ok=True)
    _commit(db, item)
    return True


def _advance(db: Database, item: RunItem, fault: FaultInjector | None) -> None:
    """Verify preconditions and publish, retire, and commit the item."""
    if item.target_path is None:
        raise JournalError("planned item has no target path")
    source = item.source_path
    target = item.target_path
    _verify_source(item, source)
    if str(source) == str(target):
        _mark_noop(db, item)
        return
    _prepare_target_dir(target)
    if target.exists() and not _is_our_target(item, target):
        raise DestinationOccupiedError("destination already exists")
    if target.exists():
        _finish_publication(db, _reload(db, item.item_id))
        return
    if _same_volume(source, target):
        _publish_same_volume(db, item, source, target, fault)
    else:
        _publish_cross_volume(db, item, source, target, fault)
    _finish_publication(db, _reload(db, item.item_id))


def _publish_same_volume(
    db: Database, item: RunItem, source: Path, target: Path, fault: FaultInjector | None
) -> None:
    """Link the source into place, record its identity, then retire it."""
    try:
        os.link(source, target)
    except FileExistsError:
        raise DestinationOccupiedError("destination appeared during publication") from None
    _set_state(db, item.item_id, ItemState.STAGED)
    _fire(fault, "after_stage")
    if not _same_file(source, target):
        raise JournalError("linked destination is not the source inode")
    _record_target(db, item, target)
    _set_state(db, item.item_id, ItemState.PUBLISHED)
    _fire(fault, "after_publish")
    _verify_source(item, source)
    os.unlink(source)
    _set_state(db, item.item_id, ItemState.SOURCE_RETIRED)
    _fire(fault, "after_retire")


def _publish_cross_volume(
    db: Database, item: RunItem, source: Path, target: Path, fault: FaultInjector | None
) -> None:
    """Stage verified bytes, publish them no-clobber, record identity, retire."""
    _ensure_preservable(source)
    stage = _stage_path(item)
    _copy_to_stage(item, source, stage)
    _set_state(db, item.item_id, ItemState.STAGED)
    _fire(fault, "after_stage")
    _verify_source(item, source)
    try:
        os.link(stage, target)
    except FileExistsError:
        raise DestinationOccupiedError("destination appeared during publication") from None
    _record_target(db, item, target)
    _set_state(db, item.item_id, ItemState.PUBLISHED)
    _fire(fault, "after_publish")
    stage.unlink(missing_ok=True)
    _verify_source(item, source)
    os.unlink(source)
    _set_state(db, item.item_id, ItemState.SOURCE_RETIRED)
    _fire(fault, "after_retire")


def _copy_to_stage(item: RunItem, source: Path, stage: Path) -> None:
    """Copy source bytes and metadata to a staging file, reusing a verified stage."""
    content_hash = item.content_hash or ""
    if stage.exists():
        if catalog.hash_file(stage) == content_hash and _source_unchanged(item):
            return
        raise PreconditionError("stale staging file")
    descriptor = os.open(stage, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle, open(source, "rb") as origin:
            shutil.copyfileobj(origin, handle, _COPY_CHUNK)
            handle.flush()
            os.fsync(handle.fileno())
        _copy_metadata(source, stage)
    except BaseException:
        stage.unlink(missing_ok=True)
        raise
    if hash_file(stage) != content_hash:
        stage.unlink(missing_ok=True)
        raise JournalError("staged bytes do not match the planned hash")


def _ensure_preservable(source: Path) -> None:
    """Fail closed when metadata cannot be carried to another filesystem.

    Extended attributes are copied; a mounted access control list cannot be, so
    an ACL, or any platform where ACLs cannot be detected, blocks the move.
    """
    if not xattr.supported():
        raise MetadataUnsupportedError("extended attributes are unsupported by this runtime")
    status = fsmeta.acl_status(source)
    if status is None:
        raise MetadataUnsupportedError("access control list detection is unavailable")
    if status:
        raise MetadataUnsupportedError("source has an access control list that cannot be preserved")


def _copy_metadata(source: Path, stage: Path) -> None:
    """Preserve mode, timestamps, and extended attributes, else fail closed."""
    try:
        stat = source.stat()
        os.chmod(stage, stat.st_mode & 0o777)
        os.utime(stage, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            raise MetadataUnsupportedError(
                "destination filesystem cannot preserve metadata"
            ) from None
        raise
    _copy_xattrs(source, stage)


def _copy_xattrs(source: Path, stage: Path) -> None:
    """Copy every extended attribute, failing closed on any inability to do so."""
    try:
        names = xattr.list_names(source)
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise MetadataUnsupportedError("cannot read extended attributes") from None
    for name in names:
        try:
            value = xattr.read(source, name)
        except OSError:
            raise MetadataUnsupportedError("cannot read an extended attribute") from None
        try:
            xattr.write(stage, name, value)
        except OSError:
            raise MetadataUnsupportedError("cannot preserve an extended attribute") from None


def _finish_publication(db: Database, item: RunItem) -> None:
    """Retire an unchanged source when the destination is verified, then commit."""
    source = item.source_path
    target = item.target_path
    if target is None:
        raise JournalError("item has no target path")
    if not _verified_target(item, target):
        raise JournalError("destination is not verified before retirement")
    if source.exists():
        _verify_source(item, source)
        os.unlink(source)
    if item.state is not ItemState.SOURCE_RETIRED:
        _set_state(db, item.item_id, ItemState.SOURCE_RETIRED)
    _commit(db, item)


def _commit(db: Database, item: RunItem) -> None:
    """Commit the item and relocate its catalog location and owning root."""
    _relocate(db, item)
    _set_state(db, item.item_id, ItemState.COMMITTED)


def _relocate(db: Database, item: RunItem) -> None:
    """Point the catalog location at the destination and its owning root."""
    target = item.target_path
    if item.location_id is None or target is None or not target.exists():
        return
    owner = roots.containing_root(db, target)
    if owner is None:
        return
    stat = target.stat()
    catalog.update_location_path(
        db,
        item.location_id,
        path=target,
        root_id=owner.root_id,
        dev=stat.st_dev,
        ino=stat.st_ino,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _mark_noop(db: Database, item: RunItem) -> None:
    """Record an already-correct path as a no-op without touching the file."""
    if item.location_id is not None:
        catalog.mark_presence(db, item.location_id, Presence.PRESENT)
    _set_state(db, item.item_id, ItemState.NOOP)


def _verify_source(item: RunItem, source: Path) -> None:
    """Verify the source still exists and still matches the planned identity."""
    if not source.exists():
        raise PreconditionError("source is missing")
    stat = source.stat()
    if item.source_dev is not None and stat.st_dev != item.source_dev:
        raise PreconditionError("source identity changed")
    if item.source_ino is not None and stat.st_ino != item.source_ino:
        raise PreconditionError("source identity changed")
    if item.source_size is not None and stat.st_size != item.source_size:
        raise PreconditionError("source size changed")
    if item.content_hash and hash_file(source) != item.content_hash:
        raise PreconditionError("source content changed")


def _source_unchanged(item: RunItem) -> bool:
    """Return whether the source still matches the planned identity."""
    try:
        _verify_source(item, item.source_path)
    except PreconditionError:
        return False
    return True


def _record_target(db: Database, item: RunItem, target: Path) -> None:
    """Journal the destination's device and inode identity."""
    stat = target.stat()
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE run_items SET target_dev = ?, target_ino = ?, updated_at = ? WHERE item_id = ?",
            (stat.st_dev, stat.st_ino, now, item.item_id),
        )


def _target_identity_matches(item: RunItem, target: Path) -> bool:
    """Return whether a destination matches the item's journaled identity."""
    if item.target_dev is None or item.target_ino is None or not target.exists():
        return False
    stat = target.stat()
    return stat.st_dev == item.target_dev and stat.st_ino == item.target_ino


def _correlate_target(db: Database, item: RunItem) -> bool:
    """Best-effort correlation of the destination to this item before resuming.

    A journaled identity match is authoritative. A surviving staging file whose
    inode equals the destination, or a same-volume source that is the same inode
    as the destination, correlates the crash between linking and journaling the
    identity. Any other identical-bytes file is foreign and never adopted.
    """
    target = item.target_path
    if target is None or not _verified_target_hash(item, target):
        return False
    if _target_identity_matches(item, target):
        return True
    if _same_file(_stage_path(item), target):
        _record_target(db, item, target)
        return True
    if item.source_path.exists() and _same_file(item.source_path, target):
        _record_target(db, item, target)
        return True
    return False


def _is_our_target(item: RunItem, target: Path) -> bool:
    """Return whether an existing destination is a verified artifact of this item."""
    if item.state not in _OWNED_STATES:
        return False
    return _verified_target(item, target)


def _verified_target_hash(item: RunItem, target: Path) -> bool:
    """Return whether the destination exists and matches the planned hash."""
    if not target.exists() or not item.content_hash:
        return False
    return hash_file(target) == item.content_hash


def _verified_target(item: RunItem, target: Path) -> bool:
    """Return whether the destination matches the planned hash and identity."""
    if not _verified_target_hash(item, target):
        return False
    return _target_identity_matches(item, target)


def _block(db: Database, item: RunItem, category: str) -> None:
    """Mark an item blocked and open a review item."""
    _set_state(db, item.item_id, ItemState.BLOCKED, error=category)
    record_review(
        db,
        kind="operation_blocked",
        detail=category,
        revision_id=item.revision_id,
        location_id=item.location_id,
    )


def _fail(db: Database, item: RunItem, category: str) -> None:
    """Mark an item failed and open a review item."""
    _set_state(db, item.item_id, ItemState.FAILED, error=category)
    record_review(
        db,
        kind="operation_failed",
        detail=category,
        revision_id=item.revision_id,
        location_id=item.location_id,
    )


def _set_state(db: Database, item_id: str, state: ItemState, *, error: str | None = None) -> None:
    """Persist one journal transition in a short transaction."""
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE run_items SET state = ?, error = ?, updated_at = ? WHERE item_id = ?",
            (str(state), error, now, item_id),
        )


def _reload(db: Database, item_id: str) -> RunItem:
    """Return a fresh item row so journaled target identity is visible."""
    with db.read() as connection:
        row = connection.execute("SELECT * FROM run_items WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        raise JournalError("unknown item id")
    return row_to_item(row)


def _stage_path(item: RunItem) -> Path:
    """Return the deterministic staging path for an item."""
    target = item.target_path
    parent = target.parent if target is not None else item.source_path.parent
    return parent / f"{STAGE_PREFIX}{item.item_id}"


def _prepare_target_dir(target: Path) -> None:
    """Ensure the destination directory exists."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def _same_volume(source: Path, target: Path) -> bool:
    """Return whether source and destination share a filesystem."""
    try:
        return source.stat().st_dev == target.parent.stat().st_dev
    except OSError:
        return False


def _same_file(source: Path, target: Path) -> bool:
    """Return whether two paths reference the same inode."""
    try:
        left = source.stat()
        right = target.stat()
    except OSError:
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _fire(fault: FaultInjector | None, point: str) -> None:
    """Invoke an injected fault hook at a journal transition."""
    if fault is not None:
        fault(point)
