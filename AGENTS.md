# Renamr

## Overview
Python CLI that renames local files from AI-extracted metadata.

## Delivery Profile

`published` — public, publishable, reviewable.

Git policy: `feature-pr` — always work on a feature branch, never directly on `main`.

- conventional commits, PR flow, squash merge in GitHub UI
- keep README, packaging metadata, and release hygiene public-facing and intentional
- branch/commit/PR workflow → load `git-workflow` skill
- Git lifecycle: every planned package ends with a commit; final push after review; completed remote features open a PR by default

## Tech Stack
- Language: Python 3.12+
- Framework: Typer
- Dependencies: LiteLLM, Pydantic, PyPDF, PyMuPDF, Pillow, Structlog
- Database: none

## Directory Structure
```text
src/
├── renamr/           # Package source
tests/
├── unit/             # Fast isolated tests
├── integration/      # Package-level smoke tests
docs/
├── decisions/        # Architecture notes if needed later
```

## Validation

- `uv run nox` - full validation gate; run before every commit
- `nox -s lint` - docs, config, comment-only changes
- `nox -s lint typecheck` - structural changes (new modules, imports, type signatures)
- `nox -s lint typecheck test` - logic or behavior changes
- `nox -s ci` - CI-equivalent local check

## Code Quality

### Structure Limits
- Files: max 500 lines
- Functions: max 50 lines
- Classes: max 100 lines
- Line Length: 100 characters

### Type Hints
All new functions require complete type hints.

### Docstrings
Use Google-style docstrings for public modules, functions, and classes.

### Error Handling
- Raise specific exceptions
- Validate inputs early
- Keep messages descriptive
- Use context managers for files and external resources

### Naming Conventions
- Variables and functions: snake_case
- Classes: PascalCase
- Constants: UPPER_SNAKE_CASE
- Private helpers: _leading_underscore
- Type aliases: PascalCase

## Conventions
- Keep the implementation simple and sync-first.
- Keep config flat and explicit.
- Prefer small focused helpers over framework-style abstractions.

## Library Documentation

Context7 MCP is available in this project. When working with external libraries, prefer
version-specific docs from Context7 instead of relying on memory.

## Architecture Decisions
- Use src/ layout for packaging.
- Use Typer for the CLI surface.
- Use LiteLLM so provider changes stay config-only.
- Keep preview extraction, metadata parsing, file operations, and CLI wiring in separate modules.

## Known Constraints
- File contents are sent to an external LLM provider; README must keep that explicit.
- Runtime data lives under `data/` and should stay out of versioned outputs.
- Undo support only covers the most recent successful rename run.

## Project Contract

Status: not-required
Manifest: TECHNICAL_CONTRACT.md
Activation reason: none
Opt-out reason: none

## GitHub Issues

Issues: on
Repo: spignotti/renamr
Project: spignotti/1
