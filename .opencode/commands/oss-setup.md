---
description: Apply OSS hardening to this Python project — CI, release workflow, cliff.toml, license, badges, branch protection
agent: build
---
Apply the OSS hardening layer to this Python OSS repository.

Load the local `python-oss-setup` skill and execute the full hardening flow.

Guards — stop immediately if:
- currently on `main` — create a feature branch first: `/git feature oss-setup`
- `pyproject.toml` is missing — run `/python-init` first
- `uv run nox` is not passing — fix the base setup before hardening

The skill will:
1. resolve author, year, and GitHub remote metadata
2. create `LICENSE` (MIT), `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `cliff.toml`
3. insert CI and PyPI badges into `README.md`
4. verify: `uv run nox` must pass
5. commit all OSS hardening as one clean logical change
6. apply branch protection via `gh api` (automated where `gh` is authenticated)
7. report what was done and print the PyPI Trusted Publisher manual checklist

Do not implement product features in this step.
After this command completes, open a PR and merge it, then configure PyPI Trusted Publisher.
