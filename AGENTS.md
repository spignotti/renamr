# Renamr

## Overview
Python CLI that renames local files from AI-extracted metadata.

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

## Architecture Decisions
- Use src/ layout for packaging.
- Use Typer for the CLI surface.
- Use LiteLLM so provider changes stay config-only.
- Keep preview extraction, metadata parsing, file operations, and CLI wiring in separate modules.

## Known Constraints
- File contents are sent to an external LLM provider; README must keep that explicit.
- Runtime data lives under `data/` and should stay out of versioned outputs.
- Undo support only covers the most recent successful rename run.
