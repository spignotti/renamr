# renamr

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
[![PyPI](https://img.shields.io/pypi/v/renamr.svg)](https://pypi.org/project/renamr/)
[![CI](https://github.com/spignotti/renamr/actions/workflows/ci.yml/badge.svg)](https://github.com/spignotti/renamr/actions/workflows/ci.yml)

Local-first document catalog and cautious file routing for an ordinary
filesystem. renamr reads pages locally, catalogs them in SQLite, proposes names
and destinations, and changes files only through an explicit, journaled plan.

Version 2 is a breaking release: inference is local Ollama only, active settings
live in SQLite, default runs preview, and PDF compression and cloud providers are
removed.

## What it does

- Onboards selected existing roots and builds a searchable page/fact catalog.
- Extracts all pages locally (embedded PDF text, OCR, text files) with real
  quality and provenance.
- Routes only unambiguous, active-rule matches; uncertain documents stay in place
  behind a focused review question.
- Learns from confirmed labels and unique manual moves. Draft rules are tested
  against positives, negatives, and conflicts before explicit activation.
- Proposes folder/structure improvements and applies them only after an explicit
  preview and confirmation.
- Builds optional source-backed contract overviews from complete stored page text.
- Exposes a tiny read-only MCP surface with per-contract, per-field local grants.
- Runs a bounded macOS reconciliation service that catches up after sleep.

Original document bytes are never rewritten. SQLite stores the catalog, settings,
rules, decisions, contract facts, and operation history; the bootstrap TOML only
locates the private state directory.

## Install

```bash
uv tool install renamr        # or: pip install renamr
```

Python 3.12+. A local Ollama installation is required for inference; renamr never
downloads models or OCR weights during document processing.

```bash
ollama pull gemma4:e4b
ollama serve                   # or let renamr start an owned server for real work
```

## Quick start

```bash
# Create the private state directory and v2 bootstrap.
renamr init

# Register selected existing roots and catalog them without changing documents.
renamr onboard --inbox ~/Documents/inbox \
  --library ~/Documents/areas --archive ~/Documents/archive \
  --describe "$HOME/Documents/areas=Reference material"

# Preview routing. This is the default; no document is moved.
renamr run

# Apply the displayed, journaled plan.
renamr run --apply

# Inspect and resolve pending questions.
renamr review

# Reverse the latest committed run through the journal.
renamr undo --preview
renamr undo --yes
```

The `--inbox` override must name a registered root; it never expands read scope.

## Local model and page extraction

Defaults use the installed `gemma4:e4b` for the `main`, `small`, and
`maintenance` roles. Roles and keep-alive are database settings:

```bash
renamr config set model.main gemma4:e4b
renamr config set model.small gemma4:e4b
renamr config set keep_alive.main 5m
renamr config set output_mode validated_prompt
```

The verified installed model digest is part of the feature-cache key, so a model
tag whose bytes changed does not reuse stale features. A missing model or digest
is reported before processing; there is no silent fallback and no download.

## Commands

| Command | Outcome |
|---|---|
| `init` | Create private state/bootstrap, or preview/apply an explicit v1 TOML import. |
| `config show`, `config set`, `config root`, `config folder` | Typed database settings, root descriptions, folder overrides. |
| `doctor [--redact]` | Integrity, roots, OCR assets, model availability, backlog. |
| `onboard` | Register sibling roots, descriptions, trusted/unsorted status, catalog. |
| `run [--apply]` | Preview by default; apply only with `--apply`. |
| `review` | Focused questions, one-off labels, manual-move confirmation. |
| `rules list/suggest/test/activate/revise/disable/rollback/history/maintain` | Bounded rule lifecycle. |
| `chat` | Five schema-bound local intents; proposals require terminal confirmation. |
| `structure propose/show/list/apply` | Explicit folder/rule plans; no deletion of old folders. |
| `search`, `document show/export` | Local full-text search, evidence, explicit text export. |
| `contracts preview/configure/refresh/show/alias/assign/fact` | Source-backed contract grouping and facts. |
| `service render/install/status/remove/pause/resume/tick` | Bounded LaunchAgent reconciliation. |
| `mcp serve`, `permissions grant/revoke/list` | Read-only stdio sharing with enumerated grants. |
| `benchmark [--live]`, `version` | Explicit local acceptance corpus and package version. |

`--compress` is rejected with a migration message because v2 preserves original
bytes. `--no-compress` is a documented no-op. `--recursive` is accepted for
compatibility; v2 scans registered roots recursively.

## Rules and learning

Rules use a frozen bounded grammar: root plus optional subtree, integer priority,
at most eight predicates over `document_type`, `issuer`, `entity_id`, or
`language`, an allowed target root/relative directory, and a template from
`{date}`, `{sender}`, `{subject}`, `{document_type}`. No regex, SQL, shell, or
generated code is accepted.

A confirmed one-off label is stored as an example, not as a rule. A unique
manual move can become an example only when bytes and identity correlate; copied,
ambiguous, offline, or app-generated moves never become training data. `rules
suggest` is deterministic from confirmed destination examples, `rules test` shows
affected/positive/negative/conflict counts, and activation is a separate command.
A missing required naming value stays in review; renamr never silently files an
`Unknown` name.

## Contracts

Configure the convention explicitly, for example each immediate child folder is
one contract:

```bash
renamr contracts preview <root-id>
renamr contracts configure <root-id> --yes
renamr contracts refresh --root <root-id>
renamr contracts show <contract-id>
```

The deterministic reader uses complete stored page text and records explicit,
calculated, user-confirmed, unknown, and conflicting origins with page/quote
evidence. Amendments override only with their own effective date. Dates, minimum
terms, notice periods, costs, and cancellation confirmations are typed. Only two
calculations are supported (minimum end from start + minimum term; notice deadline
from end − notice days). This is not legal advice.

## Scheduled service

```bash
renamr service render              # print the plist and command, write nothing
renamr service install --yes       # explicit local install
renamr service status
renamr service pause
renamr service remove --yes        # removes only this app's own job/plist
```

The user-level LaunchAgent runs about every 60 seconds and at login. One tick takes
the global writer lock non-blockingly, skips busy overlaps, recovers the journal,
budgets work (defaults 4 documents, 4 model calls, 50 pages, about 90 seconds), and
routes only inbox roots with explicit automation enabled. Large files are deferred
to manual retry. Logs are sanitized and rotated.

## MCP sharing

```bash
renamr permissions grant --contract-id <id> --field provider --field end_date --yes
renamr mcp serve                   # stdio only; no HTTP listener
```

Start it from an MCP host with `renamr mcp serve --state-dir <private-state>`.
Default is deny. Discovery shows only safe aliases with at least one grant. Both
text and structured channels use the same typed allowlist; paths, quotes, full
text, SQL, resources, mutations, and free-form summaries are unavailable. Revoke
takes effect on the next request.

> [!WARNING]
> A cloud-hosted MCP client or model can receive every permitted response. The
> local-only inference policy does not make those responses private from that
> client. This interface also cannot protect against a client that independently
> has shell or filesystem access.

See [docs/mcp.md](docs/mcp.md).

## Privacy and filesystem behavior

- Inference and OCR run locally. Document text is sent only to the local Ollama
  endpoint; document instructions are treated as data and cannot activate rules,
  change settings, choose paths, or invoke tools.
- The SQLite catalog intentionally contains private paths, text, facts, and
  evidence. It is not encrypted by default; use FileVault/full-disk encryption and
  encrypted backups. State directories are created `0700`, private files `0600`.
- Document folders may still be synchronized independently by iCloud or another
  provider. Keeping state outside synced trees reduces accidental sync, not the
  provider's own document sync.
- Diagnostics contain opaque IDs, counts, versions, categories, and code stack
  locations, not filenames, document text, prompts, facts, or credentials.
- ACLs that cannot be preserved block a cross-volume move; the source stays
  intact. Supported mode bits, timestamps, and extended attributes are preserved
  where the destination filesystem supports them.
- Unknown facts and incomplete/weak OCR stay in review; archive location or file
  timestamps never prove a contract ended.
- Text export writes complete stored OCR/text to an explicit `0600` destination.
  Searchable-PDF derivatives with embedded text layers are a future opt-in; this
  release never rewrites signed or scanned originals.

See [docs/privacy.md](docs/privacy.md) and [docs/architecture.md](docs/architecture.md).

## v1 migration

`renamr init --legacy-config ~/.config/renamr/config.toml --config <new-bootstrap> --state-dir <state>`
prints a preview. Add `--apply` to import inbox paths, template, language, and
verified local model settings once. The legacy TOML and `undo.json` are left
untouched. Cloud models, remote endpoints, and enabled compression block the
import with actionable messages; false compression settings are ignored with a
notice.

Legacy undo records never claim to know whether current bytes changed. They are
previewed per item and require explicit confirmation before a new journal entry is
created. See [docs/migration-v1-v2.md](docs/migration-v1-v2.md).

## Development

```bash
uv sync --all-groups
uv run nox                     # lint + typecheck + tests
uv run nox -s ci               # CI-equivalent with coverage
uv run nox -s security         # dependency audit + broad S rules
```

Tests are synthetic and offline by default. Service/CLI tests use temporary roots
and mock `launchctl`; model calls are injected deterministic predictors. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Maintenance

This tool is maintained for personal use and published as-is. Bug reports are
welcome via Issues. No guaranteed response time. PRs are accepted when they align
with the local-only, journaled, no-cloud scope.

## License

MIT
