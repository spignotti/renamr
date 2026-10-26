"""Boundary tests: core hygiene, no cloud provider path, and hard size limits."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import renamr.core as core

_MAX_FILE = 500
_MAX_FUNCTION = 50
_MAX_CLASS = 100
_MAX_LINE = 100


def _core_files() -> list[Path]:
    return sorted(Path(core.__file__).parent.glob("*.py"))


def test_core_import_does_not_load_litellm() -> None:
    script = "import sys, renamr.core; print('litellm' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_core_public_api_is_importable() -> None:
    for name in ("Workspace", "Settings", "RootRole", "scan", "select_rule", "preview_routing"):
        assert hasattr(core, name)
    assert "Workspace" in core.__all__


def test_core_has_no_type_suppressions() -> None:
    offenders = [
        f"{path.name}:{index}"
        for path in _core_files()
        for index, line in enumerate(path.read_text().splitlines(), start=1)
        if "type: ignore" in line or "type:ignore" in line
    ]
    assert offenders == []


def test_core_respects_structural_limits() -> None:
    problems: list[str] = []
    for path in _core_files():
        lines = path.read_text().splitlines()
        if len(lines) > _MAX_FILE:
            problems.append(f"file {path.name}={len(lines)}")
        problems.extend(
            f"line {path.name}:{index}"
            for index, line in enumerate(lines, start=1)
            if len(line) > _MAX_LINE
        )
        for node in ast.walk(ast.parse("\n".join(lines))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node.end_lineno is None:
                continue
            span = node.end_lineno - node.lineno + 1
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and span > _MAX_FUNCTION:
                problems.append(f"func {path.name}:{node.name}={span}")
            if isinstance(node, ast.ClassDef) and span > _MAX_CLASS:
                problems.append(f"class {path.name}:{node.name}={span}")
    assert problems == []
