# Renamr

## Overview
Local-first Python CLI: catalog documents on ordinary filesystems, propose
cautious routing/renaming, learn from confirmed decisions, and share a tiny
read-only contract fact surface over stdio MCP.

## Delivery Profile

`published` — public, publishable, reviewable.

Git policy: `feature-pr` — always work on a feature branch, never directly on
`main`.

- conventional commits, PR flow, squash merge in GitHub UI
- keep README, packaging metadata, and release hygiene public-facing and intentional
- branch/commit/PR workflow → load `git-workflow` skill

## Tech Stack
- Language: Python 3.12+
- Framework: Typer
- Dependencies: MCP SDK, Pydantic, PyMuPDF, Pillow, RapidOCR, Structlog
- Database: SQLite (standard library), owned by the app in a private state dir
- Inference: local Ollama only, loopback endpoint, verified installed models

## Directory Structure
```text
src/
├── renamr/           # Package source
│   ├── cli/          # Typer registrar and focused command modules
│   ├── core/         # Catalog, extraction, rules, journal, contracts, service, MCP
│   ├── mcp_server.py
│   └── service_worker.py
tests/
├── unit/             # Fast isolated tests, synthetic fixtures
├── integration/      # Package-level smoke tests
docs/
├── decisions/        # Accepted and superseded architecture decisions
├── architecture.md
├── migration-v1-v2.md
├── privacy.md
├── service.md
├── mcp.md
```

## Validation

- `uv run nox` - full validation gate; run before every commit
- `nox -s lint` - docs, config, comment-only changes
- `nox -s lint typecheck` - structural changes (new modules, imports, type signatures)
- `nox -s lint typecheck test` - logic or behavior changes
- `nox -s ci` - CI-equivalent local check
- `nox -s security` - dependency audit and broad `S` rules (advisory baseline may
  include test-only `S101`; fix real security causes, do not weaken configs)

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
- Keep the implementation simple and sync-first; async only where the MCP SDK requires it.
- Keep settings flat and explicit.
- Prefer small focused helpers over framework-style abstractions.
- No `type: ignore`, `noqa`, or relaxed lint/type/security configuration.

## Library Documentation

Context7 MCP is available in this project. When working with external libraries,
prefer version-specific docs from Context7 instead of relying on memory.

## Architecture Decisions
- Use `src/` layout for packaging.
- Use Typer for the CLI surface.
- SQLite owns settings, roots, catalog, rules, decisions, contracts, journalled
  runs, and MCP grants. Bootstrap TOML only locates private state.
- Local Ollama only: no LiteLLM, cloud providers, remote endpoints, model downloads,
  PDF compression, web UI, vector search, agent tools, or automatic delete/dedup.
- All filesystem mutation goes through preview/journal/apply/recovery and the
  cross-process writer lock; originals stay byte-identical.
- MCP is stdio-only, read-only, default-deny, per-alias/per-field enumerated.

## Known Constraints
- SQLite is not encrypted by default; explain FileVault and encrypted backups.
- Document folders may still be synchronized by iCloud or another provider.
- Extended attributes are copied where supported; unsupported ACLs block a
  cross-volume move and leave the source intact.
- Undo reverses committed journal entries; legacy `undo.json` needs explicit
  per-item confirmation.
- Contract facts are evidence-backed but are not legal advice.
- MCP permitted responses can reach cloud-hosted clients; the interface cannot
  protect a client that already has shell/filesystem access.

## GitHub Issues

Issues: on
Repo: spignotti/renamr
Project: spignotti/1
