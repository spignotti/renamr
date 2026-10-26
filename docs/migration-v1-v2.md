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

## What imports

- Existing inbox paths that still exist, as `inbox` roots.
- Language list, filename template, and loopback endpoint.
- Local Ollama model into `main`, `small`, and `maintenance` roles.

The legacy vision role is folded into the main role with a non-blocking notice.

## What is rejected

- Cloud or remote model references, with a message naming the setting.
- Non-loopback `api_base`.
- Enabled PDF compression, because v2 preserves original bytes.
- False compression settings are ignored with a notice.

A blocked preview imports nothing.

## Library and archive

v1 only knew inbox paths. Register additional library/archive roots with
`renamr onboard` before enabling routing. Allowed targets default to library and
archive roots selected in the same onboarding call.

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
