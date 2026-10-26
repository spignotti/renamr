"""Shared synthetic helpers for acceptance regression tests."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic import BaseModel
from typer.testing import CliRunner

from renamr.core import dialog, workflow
from renamr.core.extract_runner import InProcessRunner
from renamr.core.features import FeatureExtractor
from renamr.core.roots import Root, RootRole
from renamr.core.workspace import Workspace
from renamr.local_transport import ChatMessage, ChatMetrics, OutputMode, StructuredResult

runner = CliRunner()


class FakeDialogCompleter:
    """Return one canned dialog response and capture its messages."""

    def __init__(self, response: dialog.DialogResponse) -> None:
        self.response = response
        self.messages: list[ChatMessage] = []

    def complete[ResultT: BaseModel](
        self,
        model: str,
        messages: Sequence[ChatMessage],
        schema: type[ResultT],
        *,
        output_mode: OutputMode | None = None,
        images: Sequence[str] = (),
        require_vision: bool = False,
        keep_alive: str | None = None,
    ) -> StructuredResult[ResultT]:
        self.messages = list(messages)
        return StructuredResult[ResultT](
            data=cast("ResultT", self.response),
            metrics=ChatMetrics(),
            output_mode=output_mode or "validated_prompt",
        )


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace.bootstrap(tmp_path / "state", config_path=tmp_path / "config.toml")


def _roots(tmp_path: Path) -> tuple[Workspace, Root, Root, Root]:
    workspace = _workspace(tmp_path)
    inbox, library, archive = tmp_path / "Inbox", tmp_path / "Library", tmp_path / "Archive"
    for folder in (inbox, library, archive):
        folder.mkdir()
    inbox_root = workflow.register_root(workspace, inbox, role=RootRole.INBOX)
    library_root = workflow.register_root(workspace, library, role=RootRole.LIBRARY)
    archive_root = workflow.register_root(workspace, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(
        workspace, inbox_root.root_id, [library_root.root_id, archive_root.root_id]
    )
    return workspace, inbox_root, library_root, archive_root


def _scan(workspace: Workspace, root_id: str, extractor: FeatureExtractor | None = None) -> None:
    workspace.runner = InProcessRunner()
    workspace.extractor = extractor
    workflow.scan(workspace, root_id)
