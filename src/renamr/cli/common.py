"""Shared CLI helpers: workspace bootstrap, inference lifecycle, and errors."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from renamr.core import catalog, paths, processing, workflow
from renamr.core.db import Database
from renamr.core.errors import ConfigError, CoreError, InferenceError, StateError
from renamr.core.features import LocalFeatureExtractor
from renamr.core.inference import LocalInference, verify_model
from renamr.core.process import OwnedServer
from renamr.core.settings import Settings
from renamr.core.workspace import Workspace

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="Explicit v2 bootstrap TOML (format_version + state_dir)."),
]
StateOption = Annotated[
    Path | None,
    typer.Option("--state-dir", help="Override the private state directory for this command."),
]
YesOption = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Confirm this explicitly requested local action."),
]


class CliError(Exception):
    """A CLI-facing error with an already sanitized message."""


def resolve_state_dir(config_path: Path | None, state_dir: Path | None) -> Path:
    """Resolve the private state directory without creating it.

    An explicit config must be a v2 bootstrap. An old default legacy TOML is left
    untouched and the platform default state directory is used instead.
    """
    if state_dir is not None:
        return Path(state_dir).expanduser()
    candidate = paths.resolve_bootstrap_config(config_path)
    if candidate is not None:
        try:
            return paths.load_bootstrap(candidate).state_dir
        except ConfigError as exc:
            if config_path is not None:
                raise CliError(
                    f"{candidate} is not a v2 bootstrap; import it with "
                    "`renamr init --legacy-config <path> --apply`"
                ) from exc
    return paths.default_state_dir()


def private_state_dir(config_path: Path | None, state_dir: Path | None) -> Path:
    """Return an existing private state directory or raise."""
    state = resolve_state_dir(config_path, state_dir)
    if not (state / paths.CATALOG_FILENAME).is_file():
        raise CliError("catalog is not initialized; run `renamr init` first")
    return state


def open_workspace(
    config_path: Path | None, state_dir: Path | None, *, require_initialized: bool = True
) -> Workspace:
    """Open a workspace for a CLI command."""
    state = (
        private_state_dir(config_path, state_dir)
        if require_initialized
        else resolve_state_dir(config_path, state_dir)
    )
    try:
        return Workspace.open(config_path=config_path, state_dir=state)
    except (CoreError, StateError, ConfigError) as exc:
        raise CliError(_describe(exc)) from exc


def db_for_read(config_path: Path | None, state_dir: Path | None) -> Database:
    """Return a database handle for a read-only command without creating state."""
    state = private_state_dir(config_path, state_dir)
    return Database(state / paths.CATALOG_FILENAME)


def build_inference(settings: Settings, *, role: str = "main") -> LocalInference:
    """Build a local inference adapter for one role."""
    return LocalInference.from_settings(settings, role=role)


@contextmanager
def inference_session(settings: Settings, *, role: str = "main") -> Iterator[LocalInference]:
    """Start/reuse a local server only for this bounded inference work."""
    server = OwnedServer(endpoint=settings.endpoint, startup_timeout=15)
    try:
        started = server.start_if_needed()
    except (OSError, RuntimeError, TimeoutError, CoreError) as exc:
        raise CliError(
            "local Ollama server is unavailable; start it explicitly with `ollama serve` "
            "or install Ollama (renamr never downloads models)"
        ) from exc
    try:
        yield build_inference(settings, role=role)
    finally:
        if started:
            server.stop()


def verify_and_key(workspace: Workspace, settings: Settings, inference: LocalInference) -> None:
    """Verify the main local model and stamp its installed digest into the workspace."""
    model = settings.model_roles.main
    try:
        info = verify_model(inference, model)
    except InferenceError as exc:
        raise CliError(
            f"local model '{model}' is not verified ({exc.category}); install it explicitly "
            "with `ollama pull` and configure a local model"
        ) from exc
    if not info.digest:
        raise CliError(
            f"local model '{model}' reports no installed digest; "
            "feature cache keys cannot be trusted"
        )
    workspace.model_digest = info.digest


def feature_extractor(settings: Settings, inference: LocalInference) -> LocalFeatureExtractor:
    """Return the guarded feature extractor for the main role."""
    return LocalFeatureExtractor(
        inference,
        settings.model_roles.main,
        output_mode=settings.output_mode,
        max_chars=settings.feature_max_chars,
    )


def needs_feature_work(workspace: Workspace, root_ids: list[str], settings: Settings) -> bool:
    """Return whether any catalogued revision still needs a feature pass."""
    prefix = (
        f"{settings.model_roles.main}|{settings.output_mode}|{processing.FEATURE_PROMPT_VERSION}|"
    )
    for root_id in root_ids:
        for location in catalog.locations_for_root(workspace.db, root_id):
            revision = catalog.get_revision(workspace.db, location.revision_id)
            if revision.quality_state not in (catalog.QualityState.OK, catalog.QualityState.LOW):
                continue
            if revision.failed_stage == "features" or revision.next_retry_at:
                continue
            if not revision.feature_key.startswith(prefix):
                return True
    return False


def catalog_then_features(workspace: Workspace, root_ids: list[str], settings: Settings) -> bool:
    """Catalog pages without a model, then run features only if work exists.

    Returns:
        Whether an owned model server was started for the feature pass.
    """
    for root_id in root_ids:
        workflow.scan(workspace, root_id)
    if not needs_feature_work(workspace, root_ids, settings):
        return False
    with inference_session(settings, role="main") as inference:
        verify_and_key(workspace, settings, inference)
        workspace.extractor = feature_extractor(settings, inference)
        for root_id in root_ids:
            workflow.scan(workspace, root_id)
    return True


def confirm(prompt: str, *, yes: bool, input_text: str | None = None) -> bool:
    """Return explicit confirmation without inventing a noninteractive yes."""
    if yes:
        return True
    if input_text is not None:
        return input_text.strip().lower() in {"y", "yes"}
    return typer.confirm(prompt, default=False)


def describe_error(exc: Exception) -> str:
    """Return a sanitized one-line error description."""
    if isinstance(exc, CliError):
        return str(exc)
    if isinstance(exc, CoreError):
        return f"{exc.category}: {exc}"
    return f"{type(exc).__name__}: operation failed; see the local diagnostic log"


def _describe(exc: CoreError) -> str:
    """Return a sanitized core error message."""
    return f"{exc.category}: {exc}"
