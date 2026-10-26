# Migrating v1 to v2

v2 changes configuration ownership, inference, mutation semantics, and the
default state location. It does not modify the legacy TOML or `undo.json`.

## Preview and apply

```bash
renamr init \
  --legacy-config ~/.config/renamr/config.toml \
  --config ~/.config/renamr/v2-bootstrap.toml \
  --state-dir "$HOME/Library/Application Support/renamr"
```

This prints roots, languages, template, and local model settings. Add `--apply` to
import once. The database records the migration; repeated imports are refused.

The old default `config.toml` is detected and left untouched. If it remains at the
default path, it is ignored for bootstrap resolution and the platform default
state directory is used. Use a separate new bootstrap path as above for clarity.

An explicit `--config` path that does not exist fails closed instead of falling
back to the default state. Creating a new bootstrap with `init --config NEW`
remains supported.

## What imports

- Existing inbox paths that still exist, as `inbox` roots.
- Language list, filename template, and loopback endpoint.
- Local Ollama model into `main`, `small`, and `maintenance` roles.

The legacy vision role is folded into the main role with a non-blocking notice.

## What is rejected

- Cloud or remote model references, with a message naming the setting.
- Non-loopback `api_base`.
- Enabled PDF compression, because v2 preserves original bytes.
- Unsupported legacy `filename_template` values that use anything except
  `{date}`, `{sender}`, `{subject}`, and `{document_type}`.
- False compression settings are ignored with a notice.

A blocked preview imports nothing. Preview validates the entire merged settings
payload before any root, catalog, or bootstrap write, so an invalid legacy value
cannot leave a state that fails on every later command.

## Library and archive

v1 only knew inbox paths. Register additional library/archive roots with
`renamr onboard` before enabling routing. Onboarding classifies only the scopes
selected in that call with the local `small` role, extends only those inboxes'
targets, and seeds conservative placement examples from trusted library/archive
folders. It does not scan or rewire unrelated registered roots.

## Legacy undo

`undo.json` has no historical hashes. The v2 legacy adapter:

- validates record shape, regular files, registered-root boundaries, and vacant
  original paths;
- never claims the current target is unchanged;
- requires explicit per-item confirmation;
- snapshots current bytes/hash before creating a new journal entry;
- never overwrites, discards, or pretends to restore pre-compression bytes.

## After migration

```bash
renamr doctor
renamr onboard --inbox <existing> --library <root> --archive <root>
renamr run                 # preview
renamr review              # resolve questions
renamr run --apply         # explicit mutation
```

Rule activation, structure application, service installation, and MCP grants all
remain separate explicit actions.
