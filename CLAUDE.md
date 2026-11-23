# Renamr

Python CLI that renames local files from AI-extracted metadata. Public, publishable, reviewable: keep README, packaging metadata and release hygiene public-facing and intentional. Contract: `TECHNICAL_CONTRACT.md`.

## Stack
Python 3.12+, Typer, LiteLLM, Pydantic, PyPDF, PyMuPDF, Pillow, Structlog. `src/` layout, tests in `tests/unit` and `tests/integration`. Issues on, repo `spignotti/renamr`.

## Validation
- `uv run nox` is the full gate; run before every commit.
- Docs, config, comment-only: `nox -s lint`. Structural changes: `nox -s lint typecheck`. Logic or behaviour: `nox -s lint typecheck test`. CI-equivalent: `nox -s ci`.

## Rules
- Squash merge.
- Limits: files 500 lines, functions 50, classes 100, line length 100.
- Complete type hints on new functions; Google-style docstrings for public modules, functions and classes.
- Raise specific exceptions, validate inputs early, keep messages descriptive, use context managers for files and external resources.
- Naming: snake_case, PascalCase classes, UPPER_SNAKE_CASE constants, `_private` helpers.
- Keep it simple and sync-first; flat, explicit config; small focused helpers over framework-style abstractions. Keep preview extraction, metadata parsing, file operations and CLI wiring in separate modules. LiteLLM keeps provider changes config-only.
- File contents go to an external LLM provider; the README must keep that explicit.
- Runtime data lives under `data/` and stays out of versioned outputs. Undo covers only the most recent successful rename run.
