---
name: python-oss-setup
description: Harden a Python OSS repository after python-setup — adds CI, release workflow, cliff.toml, license, badges, and automated branch protection
---
## Purpose

Use this skill after `python-setup` has been applied and the base Python stack is working.

This is the second-step hardening layer for public Python OSS projects only. Run it from a feature branch, not from `main` — the changes should go through a PR.

Do not use this skill for portfolio or tool repositories.

## Preconditions

- `pyproject.toml` exists and `uv run nox` passes cleanly
- git is initialized with at least one commit
- a GitHub remote is configured (`git remote -v`)
- `gh` is installed and authenticated (`gh auth status`)

## What This Skill Adds

- MIT `LICENSE`
- `.github/workflows/ci.yml` — runs `uv run nox` on push and pull request
- `.github/workflows/release.yml` — tag-triggered release and PyPI publish workflow
- `cliff.toml` — changelog generation with Conventional Commits
- CI and PyPI badges inserted into `README.md`
- automated branch protection via `gh api`
- PyPI Trusted Publisher manual checklist

## OSS Hardening Flow

### 1. Guards

```bash
git branch --show-current    # must NOT be main — stop if it is
git rev-parse --show-toplevel  # must be a git repo
```

If on `main`: stop and tell the user to create a feature branch first with `/git feature oss-setup`, then rerun `/oss-setup`.

Verify `pyproject.toml` exists. If not, stop — run `/python-init` first.

### 2. Resolve Metadata

```bash
git config user.name          # author name
date +%Y                      # current year
gh repo view --json owner,name  # owner and repo name for badges and branch protection
```

Extract project name from `pyproject.toml` `[project] name`.

### 3. Create LICENSE

MIT license with resolved author and year:

```
MIT License

Copyright (c) <year> <author>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 4. Create `.github/workflows/ci.yml`

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv run nox
```

Job name is `check`. The branch protection status check string will be `CI / check`. Keep this consistent.

### 5. Create `.github/workflows/release.yml`

```yaml
name: Release

on:
  push:
    tags:
      - "v*"

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: release
    permissions:
      id-token: write
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv run nox
      - run: uv build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

Requires PyPI Trusted Publisher to be configured (see manual checklist below). The `environment: release` matches the Trusted Publisher environment name.

### 6. Create `cliff.toml`

```toml
[changelog]
header = "# Changelog\n"
body = """
{% if version %}\
## [{{ version | trim_start_matches(pat="v") }}] - {{ timestamp | date(format="%Y-%m-%d") }}
{% else %}\
## [unreleased]
{% endif %}\
{% for group, commits in commits | group_by(attribute="group") %}
### {{ group | strtitle }}
{% for commit in commits %}
- {{ commit.message | upper_first }}\
{% endfor %}
{% endfor %}\n
"""
trim = true

[git]
conventional_commits = true
filter_unconventional = true
split_commits = false
commit_parsers = [
  { message = "^feat", group = "Features" },
  { message = "^fix", group = "Bug Fixes" },
  { message = "^refactor", group = "Refactoring" },
  { message = "^docs", group = "Documentation" },
  { message = "^test", group = "Testing" },
  { message = "^chore", group = "Miscellaneous" },
  { message = "^ci", group = "CI" },
]
filter_commits = false
tag_pattern = "v[0-9].*"
```

### 7. Add README Badges

If `README.md` exists, insert the two badge lines on the line directly below the first `#` heading — do not rewrite anything else.

```markdown
[![CI](https://github.com/{owner}/{repo}/actions/workflows/ci.yml/badge.svg)](https://github.com/{owner}/{repo}/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/{name})](https://pypi.org/project/{name}/)
```

Fill `{owner}` and `{repo}` from `gh repo view`. Fill `{name}` from `pyproject.toml` project name. Do not add placeholder values — only insert when all three are known.

### 8. Verify

```bash
uv run nox
```

Must pass before committing. If anything fails, fix it before proceeding.

### 9. Commit

Stage all OSS setup files and commit:

```bash
git add LICENSE .github/ cliff.toml README.md
git commit -m "feat(ci): add oss release tooling and workflows"
```

### 10. Apply Branch Protection

```bash
gh auth status   # confirm authenticated
gh repo view --json owner,name   # resolve {owner} and {repo}
```

Apply branch protection via the GitHub API:

```bash
gh api repos/{owner}/{repo}/branches/main/protection \
  --method PUT \
  --input - <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["CI / check"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

Notes:
- `enforce_admins: false` — intentional. Allows the repo owner to push the release commit directly to `main` without a PR. CI still runs on the tag push before PyPI publishes. For a solo OSS repo this is the right tradeoff — strict admin enforcement would block the `/release` flow.
- `required_pull_request_reviews` is intentionally `null` — CI is the real gate, not mandatory PR reviews
- `CI / check` is the status check string from the workflow (workflow name `CI`, job name `check`)
- After the first CI run, verify this matches the name shown in GitHub → Settings → Branches → Edit rule. If it differs, update the protection rule with the correct string

On success: confirm branch protection is active.

On failure (not authenticated, API error): skip automation and include the manual branch protection steps in the final checklist.

### 11. Report

Tell the user:
- what was created (LICENSE, workflows, cliff.toml, badges, branch protection status)
- one manual step remains: PyPI Trusted Publisher setup
- release flow: run `/release` from `main` after PRs are merged

## Manual Follow-Up Checklist

PyPI Trusted Publisher (must be done in the PyPI web UI — one-time per project):
- Go to https://pypi.org/manage/account/publishing/
- Add a new publisher with these exact values:
  - Repository owner: `{owner}`
  - Repository name: `{repo}`
  - Workflow filename: `release.yml`
  - Environment name: `release`  ← must match `environment: release` in the release workflow exactly

If branch protection was not applied automatically:
- Go to GitHub → Settings → Branches → Add rule for `main`
- Enable: Require status checks to pass (`CI / check`)
- Enable: Block force pushes
- Leave PR reviews optional — CI is the gate
- Leave "Include administrators" unchecked — this allows the repo owner to push release commits directly to `main`

## Rules

- Always run from a feature branch, never from `main`
- Keep `nox` as the single CI entrypoint
- One CI workflow, one release workflow — no overlapping workflows
- Do not publish to PyPI from this command
- Do not add badges with placeholder values
- Do not rewrite the README — only insert the two badge lines
- `CHANGELOG.md` is not created here — `/release` owns it on first release

## Don't

- Run on portfolio or tool repositories
- Run from `main`
- Create release ceremony for repos that do not publish to PyPI
- Duplicate base Python setup that belongs in `python-setup`
