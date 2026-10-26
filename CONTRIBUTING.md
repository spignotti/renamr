# Contributing

## Development Setup

```bash
uv sync --all-groups
uv run nox
```

## Running Checks

```bash
uv run nox -s test
uv run nox -s lint typecheck
uv run nox -s ci
uv run nox -s security
```

## Workflow

- Create branches with `feat/` or `fix/`
- Use Conventional Commits such as `feat:`, `fix:`, `docs:`, and `test:`
- Keep changes focused and small
- Use one PR per coherent change; address review findings before merge

## Code Style

- Ruff enforces formatting and lint rules
- Pyright enforces type checking
- Respect the structure limits (500-line file, 50-line function, 100-line class,
  100-character line)
- Keep CLI parsing in `renamr/cli/`, storage/extraction/rules/journaling in
  `renamr/core/`, and SDK integration in `renamr/mcp_server.py`
- No `type: ignore`, `noqa`, or relaxed check configuration

## Tests

- All fixtures and temporary roots are synthetic. Never copy personal documents,
  private config, `.env`, or catalog exports into the repository or CI.
- Tests must not call a live model, download weights, use real `launchctl`, or
  mutate a user's documents.
- Inject deterministic offline predictors/runners; use `InProcessRunner` and
  canned OCR/structured responses.
- Use temporary `HOME`, state directories, and a mock `launchctl` for service
  tests.
- Replace obsolete v1 behavior tests with v2 safety coverage instead of deleting
  meaningful assertions.

## Local-only boundary

- v2 has no cloud/OpenRouter/LiteLLM path and no PDF compression.
- Inference endpoints must be loopback and models must be locally installed.
- Keep diagnostics sanitized: no filenames, document text, prompts, facts, or
  credentials.
- MCP changes must preserve default-deny, read-only access, and the same typed
  allowlist for text and structured channels.
