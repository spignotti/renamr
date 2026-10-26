"""P1 tests: typed settings, root boundaries, and legacy migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from renamr.core import onboarding, roots
from renamr.core.db import Database
from renamr.core.errors import ConfigError, LegacyRejectedError, PathBoundaryError, RootError
from renamr.core.migration import (
    apply_legacy_migration,
    migration_applied,
    preview_legacy_migration,
)
from renamr.core.roots import Root, RootRole
from renamr.core.settings import (
    DEFAULT_SETTINGS,
    ModelRoles,
    Settings,
    load_settings,
    reject_cloud_model,
    reject_compression,
    resolve_settings,
    save_settings,
)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "catalog.sqlite3")
    db.initialize()
    return db


def test_settings_defaults_and_roundtrip(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert load_settings(db) == DEFAULT_SETTINGS

    updated = Settings(model_roles=ModelRoles(main="llama3.1:8b"), languages=["de"])
    save_settings(db, updated)
    assert load_settings(db).model_roles.main == "llama3.1:8b"
    assert load_settings(db).languages == ["de"]


def test_settings_reject_cloud_and_remote(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ModelRoles(main="gemma4:cloud")
    with pytest.raises(ValueError):
        Settings(endpoint="https://api.openai.com")
    with pytest.raises(ValueError):
        Settings(default_filename_template="{date}_{unknown}")


def test_resolve_settings_override_is_temporary(tmp_path: Path) -> None:
    db = _db(tmp_path)
    effective = resolve_settings(db, {"languages": ["fr"]})
    assert effective.languages == ["fr"]
    assert load_settings(db).languages == ["en", "de"]
    with pytest.raises(ConfigError):
        resolve_settings(db, {"nope": 1})


def test_legacy_rejections_are_actionable() -> None:
    with pytest.raises(LegacyRejectedError, match="cloud or remote model"):
        reject_cloud_model("openai/gpt-4o")
    with pytest.raises(LegacyRejectedError, match="compression is not supported"):
        reject_compression(True)
    reject_compression(False)
    reject_cloud_model("gemma4:e4b")


def test_root_registration_and_lookup(tmp_path: Path) -> None:
    db = _db(tmp_path)
    library = tmp_path / "library"
    library.mkdir()
    root = roots.register_root(
        db, path=library, role=RootRole.LIBRARY, description="docs", state_dir=tmp_path / "state"
    )

    assert root.canonical_path == library.resolve()
    assert roots.get_root(db, root.root_id).description == "docs"
    assert [item.root_id for item in roots.list_roots(db)] == [root.root_id]
    with pytest.raises(RootError):
        roots.register_root(db, path=library, role=RootRole.INBOX, state_dir=tmp_path / "state")
    with pytest.raises(RootError):
        roots.get_root(db, "root_missing")


def test_root_rejects_state_overlap(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(RootError):
        roots.check_root_allowed(state / "inner", state_dir=state)
    assert roots.check_root_allowed(tmp_path / "library", state_dir=state) is None


def test_allowed_targets_and_boundary_resolution(tmp_path: Path) -> None:
    db = _db(tmp_path)
    source = tmp_path / "inbox"
    target = tmp_path / "archive"
    source.mkdir()
    target.mkdir()
    source_root = roots.register_root(
        db, path=source, role=RootRole.INBOX, state_dir=tmp_path / "state"
    )
    target_root = roots.register_root(
        db, path=target, role=RootRole.ARCHIVE, state_dir=tmp_path / "state"
    )

    roots.set_allowed_targets(db, source_root.root_id, [target_root.root_id])
    refreshed = roots.get_root(db, source_root.root_id)
    assert refreshed.allowed_target_ids == [target_root.root_id]
    roots.require_allowed_target(db, source_root.root_id, target_root.root_id)

    other = tmp_path / "other"
    other.mkdir()
    other_root = roots.register_root(
        db, path=other, role=RootRole.ARCHIVE, state_dir=tmp_path / "state"
    )
    with pytest.raises(PathBoundaryError):
        roots.require_allowed_target(db, source_root.root_id, other_root.root_id)


def _root_for(path: Path) -> Root:
    return Root(root_id="r", canonical_path=path, role=RootRole.LIBRARY)


def test_resolve_subpath_rejects_escapes_and_symlinks(tmp_path: Path) -> None:
    library = tmp_path / "library"
    (library / "docs").mkdir(parents=True)
    target = _root_for(library)

    assert roots.resolve_subpath(target, "docs/2026") == library / "docs" / "2026"
    assert roots.resolve_subpath(target, "") == library
    for bad in ("/abs", "..", "docs/../../escape", "docs\\win", "C:/win"):
        with pytest.raises(PathBoundaryError):
            roots.resolve_subpath(target, bad)

    (library / "link").symlink_to(library / "docs")
    with pytest.raises(PathBoundaryError):
        roots.resolve_subpath(target, "link/2026")


def test_legacy_migration_preview_and_apply_once(tmp_path: Path) -> None:
    db = _db(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config = tmp_path / "legacy.toml"
    original = (
        f'language = "de"\n'
        f'[[inbox]]\npath = "{inbox}"\n'
        f'[llm]\nmodel = "ollama/gemma4:e4b"\napi_base = "http://localhost:11434"\n'
    )
    config.write_text(original)

    preview = preview_legacy_migration(config, state_dir=tmp_path / "state")
    assert preview.blocked is False
    assert preview.model_roles is not None
    assert preview.model_roles.main == "gemma4:e4b"

    result = apply_legacy_migration(db, preview, state_dir=tmp_path / "state")
    assert result.applied is True
    assert result.roots_imported == 1
    assert migration_applied(db) is True
    assert [root.canonical_path for root in roots.list_roots(db)] == [inbox.resolve()]
    assert load_settings(db).languages == ["de"]
    assert config.read_text() == original

    with pytest.raises(ConfigError, match="already applied"):
        apply_legacy_migration(db, preview, state_dir=tmp_path / "state")


def test_legacy_migration_blocks_cloud_and_compression(tmp_path: Path) -> None:
    db = _db(tmp_path)
    config = tmp_path / "legacy.toml"
    config.write_text(
        '[[inbox]]\npath = "/tmp/x"\n'
        '[llm]\nmodel = "openai/gpt-4o"\napi_base = "https://api.openai.com"\n'
        "[compress]\nenabled = true\n"
    )
    preview = preview_legacy_migration(config, state_dir=tmp_path / "state")
    codes = {issue.code for issue in preview.issues}
    assert preview.blocked is True
    assert "cloud_model_rejected" in codes or "remote_endpoint_rejected" in codes
    assert "compression_rejected" in codes
    with pytest.raises(LegacyRejectedError):
        apply_legacy_migration(db, preview, state_dir=tmp_path / "state")


def test_wire_selected_inboxes_preserves_existing_and_adds_only_selected(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    state = tmp_path / "state"
    for name in ("inbox-a", "inbox-b", "library-a", "library-b"):
        (tmp_path / name).mkdir()
    inbox_a = roots.register_root(
        db, path=tmp_path / "inbox-a", role=RootRole.INBOX, state_dir=state
    )
    inbox_b = roots.register_root(
        db, path=tmp_path / "inbox-b", role=RootRole.INBOX, state_dir=state
    )
    library_a = roots.register_root(
        db, path=tmp_path / "library-a", role=RootRole.LIBRARY, state_dir=state
    )
    library_b = roots.register_root(
        db, path=tmp_path / "library-b", role=RootRole.LIBRARY, state_dir=state
    )
    roots.set_allowed_targets(db, inbox_a.root_id, [library_a.root_id])

    onboarding.wire_selected_inboxes(db, [inbox_a.root_id, library_b.root_id])
    assert set(roots.get_root(db, inbox_a.root_id).allowed_target_ids) == {
        library_a.root_id,
        library_b.root_id,
    }

    onboarding.wire_selected_inboxes(db, [inbox_a.root_id])
    assert set(roots.get_root(db, inbox_a.root_id).allowed_target_ids) == {
        library_a.root_id,
        library_b.root_id,
    }
    assert roots.get_root(db, inbox_b.root_id).allowed_target_ids == []
