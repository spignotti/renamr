"""Explicit local-only quality/resource benchmark command."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from renamr.benchmark import (
    build_feature_predictor,
    build_report,
    build_rule_predictor,
    run_feature_corpus,
    run_rule_corpus,
)
from renamr.benchmark_cases import CORPUS_NAME, PARA_LOCAL_V1, RULE_DIALOG_CASES
from renamr.cli.common import CliError, ConfigOption, StateOption, inference_session, open_workspace
from renamr.core.inference import verify_model
from renamr.local_transport import OutputMode


def benchmark_command(
    live: Annotated[
        bool, typer.Option("--live", help="Run the local model acceptance benchmark.")
    ] = False,
    model: Annotated[
        str | None, typer.Option("--model", help="Verified installed local model.")
    ] = None,
    output_mode: Annotated[
        str | None, typer.Option("--output-mode", help="native or validated_prompt.")
    ] = None,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Run the synthetic local acceptance corpus; never downloads a model."""
    if not live:
        typer.echo(
            f"corpus={CORPUS_NAME} feature_cases={len(PARA_LOCAL_V1)} "
            f"rule_cases={len(RULE_DIALOG_CASES)}; rerun with --live to call the local model."
        )
        return
    workspace = open_workspace(config, state_dir)
    settings = workspace.settings()
    selected_model = model or settings.model_roles.main
    mode = _mode(output_mode or settings.output_mode)
    with inference_session(settings, role="main") as inference:
        try:
            verify_model(inference, selected_model)
        except Exception as exc:
            raise CliError(f"benchmark model is unavailable locally: {type(exc).__name__}") from exc
        features = run_feature_corpus(
            build_feature_predictor(inference.client, selected_model, output_mode=mode),
            PARA_LOCAL_V1,
        )
        rules = run_rule_corpus(
            build_rule_predictor(inference.client, selected_model, output_mode=mode),
            RULE_DIALOG_CASES,
        )
    report = build_report(selected_model, features, rules, output_mode=mode)
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    if not report.passed:
        raise CliError("benchmark gate did not pass; inspect aggregate counters")


def _mode(value: str) -> OutputMode:
    """Validate the explicit output mode."""
    if value not in ("native", "validated_prompt"):
        raise CliError("output mode must be native or validated_prompt")
    return value
