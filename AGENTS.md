# Renamr

## Overview
Python CLI that renames local files from AI-extracted metadata. This repo currently contains scaffold only; implementation work starts after planning.

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
- Keep the scaffold simple; do not add product logic without a planned task.
- Prefer sync code unless there is a proven need for async.
- Keep config flat and explicit.

## Architecture Decisions
- Use src/ layout for packaging.
- Use Typer for the CLI surface.
- Use LiteLLM so provider changes stay config-only.

## Known Constraints
- iCloud handling, PDF compression, and undo support are planned but not implemented in the scaffold.
- Runtime data lives under `data/` and should stay out of versioned outputs.
