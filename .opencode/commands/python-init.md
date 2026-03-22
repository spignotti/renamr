---
description: Bootstrap this Python OSS project from PROJECT.md
agent: build
---
Initialize this Python OSS project from `PROJECT.md`.

Load the local `python-setup` skill and execute the full bootstrap process.

Guards — stop immediately if:
- `PROJECT.md` is missing in the current directory
- the directory already has a `pyproject.toml`, `setup.py`, or initialized `src/` layout — suggest `/migrate` instead

The skill will:
1. read `PROJECT.md` and extract project name, type, framework, deps, secrets, and git remote
2. initialize uv, create the package structure, install deps
3. generate `pyproject.toml` (with full OSS packaging metadata), `noxfile.py` (with `ci` session), `.gitignore`, `README.md`, `AGENTS.md`
4. write starter files and one passing smoke test
5. initialize git, commit, and push to remote if configured
6. verify: `OPENCODE_CONFIG=./opencode.json opencode --help >/dev/null` and `uv run nox` must be green

Scaffold only — do not implement product logic from PROJECT.md.

After this command completes, run `/oss-setup` to apply CI workflow, release workflow, changelog tooling, and license.
