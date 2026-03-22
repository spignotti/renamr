---
name: python-setup
description: Bootstrap a Python OSS repository from PROJECT.md — production-quality setup with uv, ruff, pyright, pytest, pytest-cov, and nox, adapted to the specific project type
---
## Purpose

Use this skill to initialize a new Python OSS project from `PROJECT.md`.

Repository category: `oss` — public, publishable, reviewable. Full toolchain, proper packaging metadata, CI-ready validation from day one.

This skill handles the base Python setup only. After it completes, run `/oss-setup` to add CI workflow, release workflow, changelog tooling, license, and badges.

## PROJECT.md Format

```markdown
# [Project Name]

## Identity
- **What**: [1 sentence — what does the project do]
- **Why**: [1 sentence — what problem does it solve]
- **Type**: [cli | library | api | data-pipeline]
- **Python**: 3.12

## Architecture
- **Framework**: [Click | Typer | FastAPI | plain | none]
- **Dependencies**: [package: why, or "none"]
- **Secrets**: [ENV_VAR_NAME: purpose, or "none"]

## Objectives
### MVP
- [ ] [Concrete, testable goal]

### Non-Goals
- [What this does not do]

## Setup
- **Category**: oss
- **Git Remote**: [https://github.com/user/repo]
```

Fields the skill reads:
- `name` → package name (kebab-case display, snake_case for Python identifier)
- `type` → directory structure
- `python` → version to pin (default `3.12`)
- `framework` → production framework dep
- `dependencies` → additional production deps
- `secrets` → `.env.example` variables
- `git_remote` → remote URL (required for OSS — remote should exist before init)

## Toolchain

- `uv` — package and environment management
- `ruff` — linting and formatting
- `pyright` — type checking (always included)
- `pytest` + `pytest-cov` — tests with coverage
- `nox` — validation entrypoint with CI session

## Bootstrap Process

### 1. Read PROJECT.md

Parse all fields. Stop immediately if `PROJECT.md` is missing. For OSS, `git_remote` should be set — warn if it is "local only" since the OSS workflow assumes a GitHub remote.

### 2. Initialize uv

```bash
uv init
uv python pin <python-version>
```

### 3. Create Package Structure

Use `src/` layout for all types.

**cli**
```
src/<package>/
├── __init__.py
├── __main__.py
├── cli.py
└── config.py
tests/
├── __init__.py
└── test_cli.py
```

**library**
```
src/<package>/
├── __init__.py
├── core.py
└── models.py
tests/
├── __init__.py
└── test_core.py
```

**api**
```
src/<package>/
├── __init__.py
├── main.py
├── routes/
│   └── __init__.py
├── models.py
└── config.py
tests/
├── __init__.py
└── test_main.py
```

**data-pipeline**
```
src/<package>/
├── __init__.py
├── pipeline.py
├── models.py
└── config.py
tests/
├── __init__.py
└── test_pipeline.py
data/               (.gitkeep)
```

### 4. Install Dependencies

```bash
uv add --dev ruff pyright pytest pytest-cov nox pip-audit
```

Framework deps:
- Click → `uv add click`
- Typer → `uv add typer`
- FastAPI → `uv add fastapi uvicorn`

Pydantic for types with config or data models:
```bash
uv add pydantic pydantic-settings
```

Any additional deps from PROJECT.md `dependencies`:
```bash
uv add <dep>
```

### 5. Generate pyproject.toml

OSS repos include full public packaging metadata:

```toml
[project]
name = "<project-name>"
version = "0.1.0"
description = "<Identity.What from PROJECT.md>"
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.12"
dependencies = []

[project.urls]
Homepage = "https://github.com/<user>/<repo>"
Repository = "https://github.com/<user>/<repo>"

[dependency-groups]
dev = [
  "nox",
  "pyright",
  "pytest",
  "pytest-cov",
  "ruff",
  "pip-audit",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "S"]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101", "S105", "S106", "S108", "S113"]

[tool.pyright]
venvPath = "."
venv = ".venv"
include = ["src", "tests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
```

Fill `name`, `description`, `requires-python`, and URL fields from PROJECT.md and `git_remote`. Production deps go into `dependencies` after `uv add`.

### 6. Generate noxfile.py

OSS repos include a `ci` session for the GitHub Actions gate and a `security` session for dependency auditing:

```python
import nox


nox.options.sessions = ["lint", "typecheck", "test", "security"]


@nox.session
def lint(session: nox.Session) -> None:
    session.run("uv", "run", "ruff", "check", "src", "tests")


@nox.session
def format(session: nox.Session) -> None:
    session.run("uv", "run", "ruff", "format", "src", "tests")


@nox.session
def fix(session: nox.Session) -> None:
    session.run("uv", "run", "ruff", "check", "--fix", "src", "tests")
    session.run("uv", "run", "ruff", "format", "src", "tests")


@nox.session
def typecheck(session: nox.Session) -> None:
    session.run("uv", "run", "pyright", "src")


@nox.session
def test(session: nox.Session) -> None:
    session.run("uv", "run", "pytest", "-v")


@nox.session
def coverage(session: nox.Session) -> None:
    session.run(
        "uv", "run", "pytest",
        "--cov=src", "--cov-report=term", "--cov-report=html",
    )


@nox.session
def ci(session: nox.Session) -> None:
    session.run("uv", "run", "ruff", "check", "src", "tests")
    session.run("uv", "run", "ruff", "format", "--check", "src", "tests")
    session.run("uv", "run", "pyright", "src")
    session.run("uv", "run", "pytest", "-v")


@nox.session
def security(session: nox.Session) -> None:
    """Security audit: dependency vulnerabilities + SAST."""
    session.install("pip-audit")
    session.run("uv", "run", "pip-audit", "--skip-editable")
    session.run("uv", "run", "ruff", "check", "--select", "S", "src", "tests")
```

### 7. Generate .gitignore

Generate based on tools in use. Always include:
```
__pycache__/
*.py[cod]
.venv/
venv/
.pytest_cache/
.ruff_cache/
.pyright/
.nox/
.coverage
htmlcov/
build/
dist/
*.egg-info/
```

Add `.env` if secrets are configured in PROJECT.md.
Nothing else — OSS repos should not have project-specific noise in `.gitignore`.

### 8. Generate .env.example

Only if `secrets` is defined in PROJECT.md:
```
# ENV_VAR_NAME: purpose
ENV_VAR_NAME=
```

Skip entirely if secrets is "none".

### 9. Generate README.md

OSS README should be presentable from day one:

```markdown
# <project-name>

> <Identity.What from PROJECT.md>

<Identity.Why — what problem this solves>

## Installation

\`\`\`bash
pip install <project-name>
\`\`\`

## Usage

<usage example based on project type>

## Development

\`\`\`bash
uv sync
uv run nox -s fix        # lint and format
uv run nox -s test       # run tests
uv run nox               # full validation
\`\`\`

## License

MIT
```

### 10. Fill AGENTS.md

The `AGENTS.md` template is already present in the project root — it was copied from the OSS template. Fill in the placeholders from PROJECT.md:

- Replace `<Project Name>` with the project name
- Replace the description placeholder with `Identity.What` and `Identity.Why` from PROJECT.md
- Replace `<version>` in Tech Stack with the Python version
- Add the framework to Tech Stack if one is configured
- Replace `<type>` in Project Type with the actual project type
- Fill in the Structure section with the actual `src/<package>/` directory name
- Add any project-specific conventions from PROJECT.md

Leave `## Known Constraints` empty — it gets filled over time.

### 11. Write Starter Files

`src/<package>/__init__.py`:
```python
"""<Identity.What from PROJECT.md>."""

__version__ = "0.1.0"
```

Entrypoint by type:

**cli** — `src/<package>/cli.py`:
```python
"""<package> CLI."""

import typer

app = typer.Typer()


@app.command()
def main() -> None:
    """<brief description>."""
    pass
```

**api** — `src/<package>/main.py`:
```python
"""<package> API."""

from fastapi import FastAPI

app = FastAPI(title="<project-name>")


@app.get("/health")
def health() -> dict[str, str]:
    """Health check."""
    return {"status": "ok"}
```

**data-pipeline** — `src/<package>/pipeline.py`:
```python
"""<package> pipeline."""


def run() -> None:
    """Run the pipeline."""
    pass
```

**library** — `src/<package>/core.py`:
```python
"""<package> core."""
```

Smoke test — `tests/test_<package>.py`:
```python
"""Smoke tests."""


def test_import() -> None:
    import <package>
    assert <package>.__version__ == "0.1.0"
```

### 12. Initialize Git

Check whether `.git` already exists:

```bash
git rev-parse --is-inside-work-tree 2>/dev/null
```

**If `.git` already exists** (repo was cloned from GitHub):
- skip `git init`
- check if remote `origin` is already set: `git remote -v`
- if remote is not set and `git_remote` is in PROJECT.md, add it: `git remote add origin <url>`
- stage and commit: `git add . && git commit -m "chore: initial project setup"`
- push: `git push -u origin main`

**If `.git` does not exist** (fresh directory):
```bash
git init
git add .
git commit -m "chore: initial project setup"
```
If `git_remote` is configured in PROJECT.md:
```bash
git remote add origin <url>
git push -u origin main
```

If push fails: stop and report init is incomplete — the OSS workflow requires a working remote. Likely causes: remote repo does not exist yet, wrong URL, auth issue.

### 13. Verify

```bash
OPENCODE_CONFIG=./opencode.json opencode --help >/dev/null
uv sync
uv run nox
```

Both checks must be green. Fix any failures before declaring success.

### 14. Summary

Report:
- project name, type, Python version, tools installed
- git status: committed on main; remote push result
- nox result

Tell the user explicitly:
```
Next steps for OSS setup:
1. Run /git feature oss-setup   ← create a feature branch first
2. Run /oss-setup               ← apply CI, release workflow, license, branch protection
3. Run /git pr                  ← open a PR
4. Merge in GitHub UI
```

Do not say "run /oss-setup" without the branch step — /oss-setup requires being on a feature branch, not main.

## Guiding principles

Scaffold only — the purpose of init is a working skeleton, not a product. Implementing business logic from PROJECT.md at this stage conflates setup with delivery and makes the scaffold harder to verify.

Create `AGENTS.md` — it's the persistent context file every agent reads at the start of every session. Without it the project has no local context and the agent has to rediscover conventions every time.

Create at least one passing smoke test — a scaffold that doesn't run a test can't prove `nox` works end-to-end. Even a trivial import test gives confidence the structure is correct.

Use `src/` layout — it prevents the common Python mistake of accidentally importing from the project root rather than the installed package, which causes subtle bugs in testing.

Use `uv add` rather than editing `pyproject.toml` directly — uv resolves and locks dependencies correctly; hand-editing can produce inconsistent lock state.

Don't create `CHANGELOG.md` — changelogs are release history. Creating one at init time is premature and `/release` will generate it correctly from git history on the first release.

Don't add deps beyond what PROJECT.md specifies — over-installing during scaffold makes it harder to understand what the project actually needs.
