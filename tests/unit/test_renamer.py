"""Renamer pipeline tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from renamr.models import AppConfig
from renamr.renamer import run


def test_run_raises_for_missing_inbox(tmp_path: Path) -> None:
    config = AppConfig(inbox_path=str(tmp_path / "missing"))

    with pytest.raises(FileNotFoundError, match="Inbox path does not exist"):
        run(config, dry_run=True, compress=False, data_dir=tmp_path / "data")
