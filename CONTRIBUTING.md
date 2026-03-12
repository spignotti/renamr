# Contributing

## Development Setup

```bash
uv sync
uv run nox
```

## Running Checks

```bash
uv run nox -s test
uv run nox -s lint typecheck
```

## Workflow

- Create branches with `feat/` or `fix/`
- Use Conventional Commits such as `feat:`, `fix:`, `docs:`, and `test:`
- Keep changes focused and small

## Code Style

- Ruff enforces formatting and lint rules
- Pyright enforces type checking
- Follow the existing module split: CLI, preview, metadata, files, renamer
