"""User-level scheduled service rendering, installation, status, and removal."""

from __future__ import annotations

import json
import sys
from typing import Annotated

import typer

from renamr.cli.common import ConfigOption, StateOption, confirm, open_workspace
from renamr.core import scheduler, workflow

service_app = typer.Typer(help="Render, install, pause, inspect, and remove the macOS service.")


@service_app.command("render")
def render_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """Print the plist and command without writing or loading anything."""
    workspace = open_workspace(config, state_dir)
    interval = workspace.settings().service.interval_s
    data = scheduler.render_agent_plist(
        python=sys.executable, state_dir=workspace.state_dir, interval_s=interval
    )
    typer.echo(f"plist: {scheduler.agent_plist_path()}")
    typer.echo(
        f"command: {sys.executable} -m renamr.service_worker --state-dir {workspace.state_dir}"
    )
    typer.echo(data.decode("utf-8"))


@service_app.command("install")
def install_command(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm local installation.")] = False,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Write and bootstrap this user's LaunchAgent after explicit confirmation."""
    workspace = open_workspace(config, state_dir)
    if not confirm(
        f"Install LaunchAgent {scheduler.LAUNCH_AGENT_LABEL} for {workspace.state_dir}?", yes=yes
    ):
        typer.echo("Service installation cancelled.")
        return
    plist = scheduler.install_agent(
        workspace.state_dir,
        interval_s=workspace.settings().service.interval_s,
    )
    typer.echo(f"Installed {plist}. Persistent personal-root activation needs its own approval.")


@service_app.command("status")
def status_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """Show job installation, pause state, last tick, and backlog."""
    workspace = open_workspace(config, state_dir)
    agent = scheduler.agent_status()
    state = scheduler.service_status(workspace)
    typer.echo(
        f"agent label={agent.label} installed={agent.installed} loaded={agent.loaded} "
        f"plist={agent.plist_path}"
    )
    typer.echo(json.dumps(state.model_dump(mode="json"), indent=2))


@service_app.command("remove")
def remove_command(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm removal.")] = False,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Unload and remove only this application's own job and plist."""
    _ = open_workspace(config, state_dir)
    if not confirm(f"Remove LaunchAgent {scheduler.LAUNCH_AGENT_LABEL}?", yes=yes):
        typer.echo("Service removal cancelled.")
        return
    removed = scheduler.remove_agent()
    typer.echo("Removed the renamr LaunchAgent." if removed else "No renamr LaunchAgent found.")


@service_app.command("pause")
def pause_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """Pause ticks without unloading the job."""
    workspace = open_workspace(config, state_dir)
    workflow.pause_service(workspace)
    typer.echo("Scheduled ticks paused.")


@service_app.command("resume")
def resume_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """Resume ticks."""
    workspace = open_workspace(config, state_dir)
    workflow.resume_service(workspace)
    typer.echo("Scheduled ticks resumed.")


@service_app.command("tick")
def tick_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """Run one bounded tick manually with sanitized counts."""
    workspace = open_workspace(config, state_dir)
    result = workflow.run_service_tick(workspace)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
