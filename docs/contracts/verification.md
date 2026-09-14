# Verification Contract

Normative evidence requirements: risk → evidence → acceptance threshold → canonical command. No blanket coverage targets or test inventories.

| Risk / decision | Evidence required | Acceptance threshold | Canonical command / artifact |
|---|---|---|---|
| Rename or undo logic changes | Behavioral tests proving filenames change correctly and the last run restores | Full gate green | `uv run nox` |
| Structural changes (new modules, imports, type signatures) | Lint plus typecheck green | Both sessions green | `uv run nox` |
| Published release readiness | Protected-branch CI green on the PR head, including dependency audit | `check` required status green | GitHub CI on the PR head |
| Secret or credential exposure | Secrets scan on staged files plus PR-range secret scan | No findings | `git diff --cached` scan plus CI secret scan |

## Rules

- Evidence must prove the reported problem changed, not merely that the build compiles.
- Suppressing a gate requires an explicit contract amendment (plan-named, user-approved).
- Tests target identified risks; do not pad coverage for its own sake.
