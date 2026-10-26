"""Bootstrap, typed configuration, and diagnostics commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from renamr import __version__
from renamr.cli.common import (
    CliError,
    ConfigOption,
    StateOption,
    open_workspace,
    resolve_state_dir,
)
from renamr.core import paths, workflow
from renamr.core.db import Database
from renamr.core.errors import ConfigError, CoreError
from renamr.core.extract import preflight_ocr_assets
from renamr.core.inference import LocalInference, local_health, verify_model
from renamr.core.migration import (
    apply_legacy_migration,
    migration_applied,
    preview_legacy_migration,
)
from renamr.core.settings import Settings, load_settings
from renamr.core.workspace import Workspace

config_app = typer.Typer(help="Show and change database-owned settings and folder overrides.")
server_app = typer.Typer(help="Show and change local model roles.")


def version_command() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def init_command(
    config: ConfigOption = None,
    state_dir: StateOption = None,
    legacy_config: Annotated[
        Path | None,
        typer.Option("--legacy-config", help="Preview an explicit v1 TOML import."),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Apply a previewed legacy import exactly once.")
    ] = False,
) -> None:
    """Create the private catalog and v2 bootstrap, or preview a v1 import."""
    if legacy_config is not None:
        _init_from_legacy(config, state_dir, legacy_config, apply=apply)
        return
    chosen = _bootstrap_path(config)
    if chosen is not None and chosen.exists():
        _existing_bootstrap(chosen)
        return
    state = resolve_state_dir(config, state_dir, allow_missing_config=True)
    workspace = Workspace.open(state_dir=state)
    if chosen is not None:
        paths.write_bootstrap(chosen, state)
        typer.echo(f"Created bootstrap {chosen}")
    else:
        typer.echo(
            "Detected a legacy default config; it was left untouched. Import it explicitly with "
            "`renamr init --legacy-config <path> --config <new-bootstrap> --apply`."
        )
    typer.echo(f"Initialized private state {state}")
    if migration_applied(workspace.db):
        typer.echo("Legacy migration is already recorded; source files were left untouched.")


@config_app.command("show")
def config_show(
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Print database-owned settings and registered roots."""
    workspace = open_workspace(config, state_dir)
    typer.echo(json.dumps(load_settings(workspace.db).model_dump(mode="json"), indent=2))
    for root in workflow.list_roots(workspace):
        typer.echo(
            f"{root.root_id} {root.role} trusted={root.trusted_examples} "
            f"automation={root.automation_enabled} alias={root.alias or '-'} "
            f"path={root.canonical_path} targets={','.join(root.allowed_target_ids) or '-'}"
        )


@config_app.command("set")
def config_set(
    key: str,
    value: str,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Set one typed database-owned setting (temporary CLI overrides do not exist)."""
    workspace = open_workspace(config, state_dir)
    current = load_settings(workspace.db).model_dump()
    _apply_setting(current, key, value)
    try:
        updated = Settings.model_validate(current)
    except ValueError as exc:
        raise CliError(f"invalid setting: {exc}") from exc
    workflow.set_settings(workspace, updated)
    typer.echo(f"Updated {key}")


@config_app.command("root")
def config_root(
    root_id: str,
    description: Annotated[str | None, typer.Option("--description")] = None,
    alias: Annotated[str | None, typer.Option("--alias")] = None,
    trusted: Annotated[bool | None, typer.Option("--trusted/--unsorted")] = None,
    automation: Annotated[bool | None, typer.Option("--automation/--no-automation")] = None,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Describe a root, mark trusted examples, or enable unattended routing."""
    workspace = open_workspace(config, state_dir)
    root = workflow.update_root(
        workspace,
        root_id,
        description=description,
        alias=alias,
        trusted_examples=trusted,
        automation_enabled=automation,
    )
    typer.echo(json.dumps(root.model_dump(mode="json"), indent=2))


@config_app.command("folder")
def config_folder(
    root_id: str,
    relative_path: str,
    description: Annotated[str | None, typer.Option("--description")] = None,
    alias: Annotated[str | None, typer.Option("--alias")] = None,
    template: Annotated[str | None, typer.Option("--template")] = None,
    target_root: Annotated[str | None, typer.Option("--target-root")] = None,
    target_subdir: Annotated[str | None, typer.Option("--target-subdir")] = None,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Persist a folder description or per-folder naming/routing override."""
    workspace = open_workspace(config, state_dir)
    override = workflow.set_folder_override(
        workspace,
        root_id,
        relative_path=relative_path,
        description=description,
        alias=alias,
        filename_template=template,
        target_root_id=target_root,
        target_subdir=target_subdir,
    )
    typer.echo(json.dumps(override.model_dump(mode="json"), indent=2))


def doctor_command(
    config: ConfigOption = None,
    state_dir: StateOption = None,
    redact: Annotated[
        bool, typer.Option("--redact", help="Show opaque IDs and categories only.")
    ] = False,
) -> None:
    """Check catalog integrity, roots, local assets, and model availability."""
    state = resolve_state_dir(config, state_dir)
    catalog_path = state / paths.CATALOG_FILENAME
    healthy = True
    typer.echo(f"state: {'<redacted>' if redact else state}")
    typer.echo(f"catalog_present: {catalog_path.is_file()}")
    if not catalog_path.is_file():
        raise CliError("catalog is not initialized; run `renamr init` first")
    database = Database(catalog_path)
    typer.echo(f"integrity: {database.integrity_check()}")
    healthy = healthy and database.integrity_check()
    workspace = open_workspace(config, state_dir)
    settings = load_settings(workspace.db)
    typer.echo(f"schema: {database.schema_version()}")
    typer.echo(f"fts5: {_fts_available(workspace)}")
    assets = preflight_ocr_assets()
    typer.echo(f"ocr_assets: {assets.available} ({assets.detail})")
    healthy = healthy and assets.available
    for root in workflow.list_roots(workspace):
        label = root.root_id if redact else str(root.canonical_path)
        typer.echo(
            f"root {label} role={root.role} available={root.canonical_path.is_dir()} "
            f"trusted={root.trusted_examples} automation={root.automation_enabled}"
        )
    reviews = workflow.review_items(workspace)
    typer.echo(f"open_reviews: {len(reviews)}")
    typer.echo(f"legacy_migration: {migration_applied(workspace.db)}")
    _model_report(settings, redact=redact)
    if config is None and not _legacy_default_absent():
        typer.echo("legacy_default_config: present (left untouched; optional explicit import)")
    if not healthy:
        raise CliError("doctor found blocking problems; see the checks above")


def _init_from_legacy(
    config: Path | None, state_dir: Path | None, legacy: Path, *, apply: bool
) -> None:
    """Preview or apply an explicit v1 TOML import."""
    chosen = _bootstrap_path(config)
    if chosen is not None and chosen.exists():
        raise CliError(f"{chosen} already exists; choose a new --config path")
    state = resolve_state_dir(config, state_dir, allow_missing_config=True)
    preview = preview_legacy_migration(legacy, state_dir=state)
    typer.echo(json.dumps(preview.model_dump(mode="json"), indent=2, default=str))
    if preview.blocked:
        raise CliError("legacy config has blocking issues; source files were not changed")
    if not apply:
        typer.echo("Preview only. Rerun with --apply to import this preview once.")
        return
    workspace = Workspace.open(state_dir=state)
    with workspace.lock():
        result = apply_legacy_migration(workspace.db, preview, state_dir=state)
    if chosen is not None:
        paths.write_bootstrap(chosen, state)
        typer.echo(f"Created bootstrap {chosen}")
    typer.echo(
        f"Imported {result.roots_imported} root(s); skipped {len(result.skipped_roots)}; "
        "legacy TOML and undo.json were left untouched."
    )


def _bootstrap_path(config: Path | None) -> Path | None:
    """Return the bootstrap destination for init, or None when a legacy file owns it."""
    if config is not None:
        return Path(config).expanduser()
    candidate = paths.config_candidates(None)[0]
    if candidate.exists():
        try:
            paths.load_bootstrap(candidate)
        except ConfigError:
            return None
    return candidate


def _existing_bootstrap(path: Path) -> None:
    """Report an existing bootstrap without overwriting it."""
    try:
        loaded = paths.load_bootstrap(path)
    except ConfigError as exc:
        raise CliError(
            f"{path} is a legacy config; use `renamr init --legacy-config {path} "
            "--config <new-bootstrap> --apply`"
        ) from exc
    typer.echo(f"{path} already exists for state {loaded.state_dir}; nothing changed.")


def _apply_setting(current: dict[str, Any], key: str, value: str) -> None:
    """Apply one allowlisted setting value to a settings dump."""
    if key == "endpoint":
        current["endpoint"] = value
    elif key == "output_mode":
        current["output_mode"] = value
    elif key == "default_filename_template":
        current["default_filename_template"] = value
    elif key == "languages":
        current["languages"] = [item.strip().lower() for item in value.split(",") if item]
    elif key.startswith("model."):
        roles = dict(current["model_roles"])
        roles[key.removeprefix("model.")] = value
        current["model_roles"] = roles
    elif key.startswith("keep_alive."):
        keep = dict(current["model_keep_alive"])
        keep[key.removeprefix("keep_alive.")] = value
        current["model_keep_alive"] = keep
    elif key.startswith("service."):
        service = dict(current["service"])
        name = key.removeprefix("service.")
        service[name] = int(value) if name != "max_seconds" else float(value)
        current["service"] = service
    elif key in {
        "max_pages",
        "max_ocr_pages",
        "max_render_pixels",
        "max_file_bytes",
        "feature_max_chars",
        "backup_retention",
        "num_predict",
        "num_ctx",
    }:
        current[key] = int(value)
    elif key in {"request_timeout_s", "extraction_timeout_s", "stability_window_s"}:
        current[key] = float(value)
    else:
        raise CliError(f"unknown or unsafe setting key: {key}")


def _fts_available(workspace: Workspace) -> bool:
    """Return whether the FTS5 index answers a trivial query."""
    try:
        with workspace.db.read() as connection:
            connection.execute(
                "SELECT count(*) FROM pages_fts WHERE pages_fts MATCH 'x'"
            ).fetchone()
    except Exception:
        return False
    return True


def _model_report(settings: Settings, *, redact: bool) -> None:
    """Report local server and model availability without starting anything."""
    inference = LocalInference.from_settings(settings)
    if not local_health(inference):
        typer.echo("model_server: unavailable (no start attempted)")
        return
    typer.echo("model_server: available")
    for role in ("main", "small", "maintenance"):
        model = getattr(settings.model_roles, role)
        try:
            info = verify_model(inference, model)
            digest = "<missing>" if not info.digest else info.digest[:12]
            typer.echo(f"model.{role}: {model} digest={digest} capabilities={info.capabilities}")
        except CoreError as exc:
            typer.echo(f"model.{role}: {model} unavailable ({exc.category})")
    _ = redact


def _legacy_default_absent() -> bool:
    """Return whether the default config candidate is absent or a v2 bootstrap."""
    candidate = paths.config_candidates(None)[0]
    if not candidate.exists():
        return True
    try:
        paths.load_bootstrap(candidate)
    except ConfigError:
        return False
    return True
