# Architecture

renamr is a synchronous Typer CLI over a local SQLite catalog. The filesystem is
the document store; SQLite owns settings, roots, document revisions, page text,
facts, rules, decisions, contracts, reviews, journals, and MCP grants.

## Modules

| Area | Modules |
|---|---|
| CLI registrar and commands | `renamr/cli/` (`bootstrap`, `onboard`, `run`, `review`, `rules`, `chat`, `structure`, `library`, `contracts`, `service`, `mcp`, `benchmark`) |
| Workspace/storage | `core/workspace.py`, `core/db.py`, `core/schema.py`, `core/paths.py`, `core/lock.py`, `core/catalog.py` |
| Extraction and inference | `core/extract.py`, `core/extract_runner.py`, `core/inference.py`, `local_client.py`, `local_transport.py`, `ollama.py`, `core/process.py` |
| Pages, facts, search | `core/processing.py`, `core/features.py`, `core/facts.py`, `core/search.py` |
| Routing, learning, review | `core/rules.py`, `core/learning.py`, `core/examples.py`, `core/onboarding.py`, `core/placement.py`, `core/review.py`, `core/folders.py` |
| Dialog/structure | `core/dialog.py`, `core/dialog_context.py`, `core/dialog_state.py`, `core/dialog_render.py`, `core/structure.py` |
| Contracts | `core/contract_fields.py`, `core/contract_types.py`, `core/contract_aggregate.py`, `core/contracts.py`, `core/contract_identity.py` |
| Mutation and history | `core/mutation.py`, `core/operations.py`, `core/publication.py`, `core/history.py`, `core/journal.py`, `core/xattr.py`, `core/fsmeta.py` |
| Service | `core/scheduler.py`, `core/budget.py`, `service_worker.py` |
| MCP | `core/mcp_gateway.py`, `mcp_server.py` |

## State and identity

Default private state:

- macOS: `~/Library/Application Support/renamr/`
- Linux: `$XDG_STATE_HOME/renamr/` or `~/.local/state/renamr/`

Bootstrap precedence is explicit `--config`, then
`$XDG_CONFIG_HOME/renamr/config.toml`, then `~/.config/renamr/config.toml`. A v2
bootstrap contains only `format_version` and `state_dir`. Active behavior lives in
the database. An old default v1 TOML is detected, never overwritten, and ignored
for bootstrap resolution; import it explicitly.

Content identity is a full SHA-256 hash. Location identity is a separate opaque
ID per canonical path, so identical copies coexist while a moved file keeps its
content. A missing root is unavailable, not empty. Page text is stored per page
with method, rotation, confidence, quality, and incompleteness.

## Safety invariants

- Only registered roots are read; only explicitly allowed target roots receive
  routed files. Canonical paths, symlink boundaries, and state overlap are
  validated before registration and again at mutation time.
- Originals are never rewritten. Compression and in-place PDF rewriting are not
  supported. Text export is a separate `0600` file, refuses an occupied
  destination unless explicitly confirmed, and never rewrites a registered
  source original.
- A preview is the default. Files change only through `run --apply`, an explicit
  structure apply, undo, or a service tick with automation enabled.
- An uncertain document stays in place with a review item. Missing naming values
  never become an automatic `Unknown` filename. A readable document with no
  matching rule produces one deduplicated destination question.
- Routine classification uses the `small` model role. Rule/structure dialog uses
  `maintenance`; `main` is retained for explicit benchmark runs. Feature cache
  keys include the role's actual model and installed digest.
- Dialog proposals fingerprint settings, rules, roots, targets, folder policy,
  structure plans, and selected-root locations, revisions, and facts. Applying a
  stale label or structure proposal fails. Pending routes re-run deterministic
  rule selection at mutation time, so a new higher-priority rule or changed facts
  invalidate the plan.
- Contract groups accept a validated root-relative base path. Stable contract ids
  follow member folders/files through approved structure plans and confirmed
  manual moves. Grounded private references attach `entity_id` only when they
  resolve to one contract; shared references stay ambiguous and private values
  never enter MCP output or diagnostics.
- Every writer takes the same `fcntl.flock` on the state lock file. The lock file
  is never unlinked or recreated while it may be held. MCP never writes.
- File operations move through planned → staged → published → source retired →
  committed (plus noop/blocked/failed). Publication never overwrites; same-volume
  moves link then retire, cross-volume moves stage and verify bytes. Recovery
  reconciles the journal before new work.
- Undo verifies both current target bytes and original-path vacancy; conflicts
  become review items.
- SQLite uses WAL, foreign keys, a bounded busy timeout, and online backups. A
  corrupt or newer-schema database fails closed. Restoration requires a stopped
  service and the exclusive writer lock, then journal/filesystem reconciliation.

## Known boundaries

- SQLite is unencrypted by default; FileVault/full-disk encryption and encrypted
  backups are the user's responsibility.
- Extended attributes are copied where supported; a detected ACL that cannot be
  preserved blocks a cross-volume move and leaves the source intact.
- iCloud evicted placeholders and in-progress files are deferred. An explicit
  onboarding may materialize a bounded set later; v2 does not download an archive
  silently.
- Searchable-PDF derivatives with embedded text layers are a future opt-in.
- Scheduled reconciliation is not a filesystem watcher; latency is about one tick.
- Contract extraction is deterministic and evidence-based, not legal advice.
- MCP permissions control this interface only. A client with shell/filesystem
  access can read the private state directly.

See `docs/decisions/` for superseded and current decisions.
