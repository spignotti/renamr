"""Workspace context: the database, private state directory, and injection seams.

``Workspace`` is a small state object. Workflows live in focused modules
(:mod:`renamr.core.workflow` for roots/scan/rules access and
:mod:`renamr.core.mutation` for preview/apply/recover/undo) and take a workspace
as their first argument.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from renamr.core.db import Database
from renamr.core.errors import ConfigError
from renamr.core.extract_runner import ExtractionRunner, SubprocessRunner
from renamr.core.features import FeatureExtractor
from renamr.core.lock import WriterLock
from renamr.core.paths import (
    BACKUPS_DIRNAME,
    CATALOG_FILENAME,
    EXPORTS_DIRNAME,
    LOGS_DIRNAME,
    default_state_dir,
    ensure_private_dir,
    load_bootstrap,
    resolve_bootstrap_config,
    write_bootstrap,
)
from renamr.core.settings import Settings, load_settings, save_settings


@dataclass
class Workspace:
    """A catalog database plus its private state directory."""

    db: Database
    state_dir: Path
    extractor: FeatureExtractor | None = None
    runner: ExtractionRunner | None = None
    model_digest: str = ""

    def settings(self) -> Settings:
        """Return the current database-owned settings."""
        return load_settings(self.db)

    def save_settings(self, settings: Settings) -> None:
        """Persist settings."""
        save_settings(self.db, settings)

    def lock(self) -> WriterLock:
        """Return a new writer lock for this state directory."""
        return WriterLock.for_state(self.state_dir)

    def extraction_runner(self) -> ExtractionRunner:
        """Return the configured runner, defaulting to the bounded subprocess one."""
        return self.runner or SubprocessRunner()

    @classmethod
    def bootstrap(
        cls,
        state_dir: Path,
        *,
        config_path: Path | None = None,
        settings: Settings | None = None,
    ) -> Workspace:
        """Create the private state directory, catalog, and optional bootstrap."""
        state = ensure_state_layout(Path(state_dir).expanduser())
        db = Database(state / CATALOG_FILENAME)
        with WriterLock.for_state(state):
            db.initialize()
        if settings is not None:
            save_settings(db, settings)
        if config_path is not None and not Path(config_path).exists():
            write_bootstrap(Path(config_path), state)
        return cls(db, state)

    @classmethod
    def open(
        cls,
        *,
        config_path: Path | None = None,
        state_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        extractor: FeatureExtractor | None = None,
        runner: ExtractionRunner | None = None,
        model_digest: str = "",
    ) -> Workspace:
        """Open a workspace, resolving the state directory by precedence."""
        state = ensure_state_layout(_resolve_state(config_path, state_dir, env))
        db = Database(state / CATALOG_FILENAME)
        with WriterLock.for_state(state):
            db.initialize()
        return cls(db, state, extractor=extractor, runner=runner, model_digest=model_digest)

    @classmethod
    def open_locked(
        cls,
        *,
        config_path: Path | None = None,
        state_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        blocking: bool = False,
        timeout: float | None = None,
        extractor: FeatureExtractor | None = None,
        runner: ExtractionRunner | None = None,
        model_digest: str = "",
    ) -> tuple[Workspace | None, WriterLock | None]:
        """Open and initialize only while holding one caller-owned writer lock.

        Returns:
            ``(workspace, held_lock)`` when initialization succeeded, or
            ``(None, None)`` when a non-blocking attempt found another writer.

        Raises:
            LockError: If a blocking wait times out or the lock cannot be opened.
            StateError: If the state layout is invalid.
        """
        state = ensure_state_layout(_resolve_state(config_path, state_dir, env))
        lock = WriterLock.for_state(state)
        if not lock.acquire(blocking=blocking, timeout=timeout):
            return None, None
        try:
            db = Database(state / CATALOG_FILENAME)
            db.initialize()
        except BaseException:
            lock.release()
            raise
        return (
            cls(db, state, extractor=extractor, runner=runner, model_digest=model_digest),
            lock,
        )


def _resolve_state(
    config_path: Path | None, state_dir: Path | None, env: Mapping[str, str] | None
) -> Path:
    """Resolve a state directory without creating it."""
    resolved = state_dir
    if resolved is not None:
        return Path(resolved).expanduser()
    if config_path is not None and not Path(config_path).expanduser().is_file():
        raise ConfigError(
            f"explicit bootstrap config does not exist: {Path(config_path).expanduser()}"
        )
    bootstrap_path = resolve_bootstrap_config(config_path, env)
    if bootstrap_path is not None:
        return load_bootstrap(bootstrap_path).state_dir
    return default_state_dir(env)


def ensure_state_layout(state_dir: Path) -> Path:
    """Create the private state directory layout and return the state directory."""
    state = ensure_private_dir(state_dir)
    for name in (BACKUPS_DIRNAME, LOGS_DIRNAME, EXPORTS_DIRNAME):
        ensure_private_dir(state / name)
    return state


def open_workspace(
    *,
    config_path: Path | None = None,
    state_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    extractor: FeatureExtractor | None = None,
    runner: ExtractionRunner | None = None,
    model_digest: str = "",
) -> Workspace:
    """Convenience wrapper around :meth:`Workspace.open`."""
    return Workspace.open(
        config_path=config_path,
        state_dir=state_dir,
        env=env,
        extractor=extractor,
        runner=runner,
        model_digest=model_digest,
    )
