# Privacy

renamr is local-first. Document text and extracted facts are stored in a private
SQLite catalog and are sent only to a loopback Ollama endpoint for this release.

## Data in the private state directory

`catalog.sqlite3` intentionally contains private canonical paths, complete page
text, extracted facts, contract evidence quotes, private contract/reference
numbers, stable contract entity ids, decisions, and operation history. Backups and
text exports contain the same private data.

SQLite is not encrypted by default. Use FileVault or another full-disk encryption
layer and encrypted backups. Directories this app creates are `0700`; private
files are `0600`. SQLite `-wal`/`-shm` sidecars are tightened where possible.

Keep the state directory outside document roots. Document folders may still be
synchronized by iCloud, Dropbox, or another provider; moving state out of them
only avoids adding catalog data to that sync.

## Inference

- Endpoint validation permits only loopback HTTP, no credentials, no query, no
  path, and no remote host.
- Model names are local names only; cloud tags and remote registry references are
  rejected. A model started by renamr gets `OLLAMA_NO_CLOUD=1`, a literal loopback
  bind, debug off, inherited proxies removed, and discarded output.
- A server owned by another application is reused, never stopped.
- Missing model/OCR assets are setup errors. Document processing never downloads
  weights.
- Document text is untrusted data. Instructions inside a document cannot activate
  rules, change settings, select arbitrary paths, or invoke tools.
- Chat uses five bounded intents and persists validated proposals/decisions, not
  raw conversation. The terminal performs every confirmation.

## Diagnostics and logs

Sanitized logs contain opaque IDs, counts, versions, stable error categories, and
code stack locations. They must not contain document text, filenames, real
document paths, prompts, model responses, contract values, private reference
numbers, or credentials. Rotated logs retain the latest three files with
owner-only permissions.

`doctor --redact` avoids printing root paths. Normal local CLI/search/contract
views may display private names and evidence deliberately; treat terminal output
and exported text as private.

## MCP and cloud clients

MCP starts with no grants. A local owner explicitly grants enumerated fields for
one safe contract alias. Both text and structured responses use the same allowlist.
Private contract/reference numbers and `entity_id` facts are not in that allowlist,
so the entity bridge never exports them.

> [!WARNING]
> A cloud-hosted MCP client or model can receive every permitted response. The
> local-only inference policy does not make those responses private from that
> client. This interface cannot protect against a client that independently has
> shell or filesystem access.

See `docs/mcp.md` for the exact surface.

## Deletion and retention

Application decisions and rule history remain until explicit maintenance because
they support undo and reproducibility. The default backup retention is three
pre-migration backups. Renamr never deletes or deduplicates documents
automatically.
