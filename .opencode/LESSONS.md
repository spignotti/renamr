# Project Lessons

Project-specific learnings for this repository. Run `/lessons` to curate entries, promote cross-project patterns to the global lessons file, and promote standing rules to `AGENTS.md`.

Entry format: `- [YYYY-MM-DD] <what was tricky or wrong> → <correct approach>`

---

## Project Decisions

<!-- one-off architectural choices, constraints, workarounds specific to this project -->

## Tooling and Environment

<!-- project-specific tool behavior, config quirks, environment setup gotchas -->
- [2026-03-22] Direct OSS releases from `main` require GitHub branch protection with `enforce_admins: false` → keep required checks enabled, but do not apply them to admins if the release flow pushes version/tag commits directly to `main`
- [2026-03-22] Dependency loggers can duplicate or overwhelm CLI output when root logging is configured too broadly → keep the root logger at `WARNING`, set the app logger level separately, and quiet noisy dependency loggers explicitly

## Recurring Issues

<!-- patterns that keep coming up in this project — candidates for promotion to global lessons -->
