# Security CI

Opt-in CI security checks for PRs against `main`. Required checks block; advisory findings do not, but are triaged; Gitleaks is fail-closing (manual merge gate until branch protection exists).

## Current status

| Check | Mode | Fails on |
|-------|------|----------|
| `CI / check` | required | any failure (`uv run nox` + `pip-audit`) |
| `CI / gitleaks` | fail-closing-manual | any finding or scanner error on the PR commit range |

## Manual enforcement note

Classic branch protection on `main` currently requires only the `check` context and was left unchanged by this additive migration. Until a separately approved protection migration adds them, `CI / check` (existing job, preserved as-is) and `CI / gitleaks` (new PR-only job) are reviewed manually on every PR via `/pr-review` and must be green before squash-merge.

## Activation gate

Run the manual full-history Gitleaks scan (`Gitleaks full history scan` workflow dispatch) before enabling the capability. A finding stops activation and requires a rotation/incident decision.

## Redaction and output

All scanner output is redacted and bounded: metadata summaries only, no raw reports, no source text, no uploaded artifacts.

## Leak remediation

1. Stop merge while Gitleaks is red.
2. Revoke/rotate the leaked secret first.
3. Remove the current value from the codebase.
4. Obtain explicit approval before any Git history rewrite.
5. Never paste a leaked value into an issue, PR, or chat.

## Triage fields

| Field | Description |
|-------|-------------|
| ID | CVE, rule ID, or misconfiguration check |
| Severity | UNKNOWN, LOW, MEDIUM, HIGH, CRITICAL |
| Fix status | fixed / unfixed |
| Applicability | Does this affect our deployment? |
| Owner | Who investigates? |
| Disposition | accept / fix / defer |
| Review date | When was this triaged? |

## Action and scanner pins

| Component | Pin | Verified |
|-----------|-----|----------|
| `actions/checkout` (gitleaks jobs) | `3d3c42e5aac5ba805825da76410c181273ba90b1` (# v7.0.1) | security-ci recipe |
| Gitleaks release | `v8.30.1` / archive `8.30.1` | `gh api repos/gitleaks/gitleaks/releases/latest` |
| Gitleaks `linux_x64` SHA-256 | `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb` | GitHub release asset digest |

**Update procedure:** Dependabot handles `uses:` pins. Inline scanner version strings are reviewed at least monthly.

## Local security session

`nox -s security` runs `pip-audit --skip-editable` plus `ruff check --select S src tests`. It is intentionally not part of the default `nox` sessions, so the existing `CI / check` job (full `uv run nox`) is unaffected until the Bandit (`S`) rule set is proven green on this codebase.
