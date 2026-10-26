# Scheduled service

renamr can install a user-level macOS LaunchAgent that performs one bounded
reconciliation tick about every 60 seconds and at login. It catches up naturally
after sleep through LaunchAgent run-at-load and interval behavior.

## Commands

```bash
renamr service render                  # print plist + command, no write/load
renamr service install --yes           # write and bootstrap this user's job
renamr service status                  # job state + pause/last tick/backlog
renamr service pause
renamr service resume
renamr service remove --yes            # removes only this app's own job/plist
renamr service tick                    # manual bounded tick
```

Installation is never a side effect of package installation, migration, or model
setup. Persistent processing of personal roots still needs the user's explicit
install approval.

## Tick behavior

- Take the global writer lock non-blockingly; a busy tick is skipped and reported.
- Reconcile non-terminal journal items before new work.
- Scan registered roots with a cheap stat fast path. Unchanged content causes no
  hash, OCR, or model call.
- Budget defaults per tick: 4 documents, 4 model calls, 50 pages, about 90
  seconds. Settings are database-owned (`service.max_documents`,
  `service.max_model_calls`, `service.max_pages`, `service.max_seconds`,
  `service.interval_s`).
- A changed file whose page count does not fit the remaining page budget is
  deferred with a `scan_budget_deferred` review item and manual retry path. It is
  never marked complete.
- Feature work starts/reuses an eligible local server only when there is actual
  work. Only an owned process is stopped. Role-specific keep-alive avoids
  unnecessary resident models; requests are sequential.
- Classify possible manual moves conservatively. Own operations are not training
  examples.
- Route only inbox roots with `automation_enabled` and active rules, through the
  same journal as an interactive apply.
- Write only sanitized logs under the private state `logs/` directory; rotation
  retains the latest files.

## Uninstall

`renamr service remove --yes` boots out the renamr label and deletes only
`com.renamr.reconcile.plist`. It does not remove unrelated LaunchAgents, document
files, or the catalog. Stop/remove any independently started Ollama process
yourself; renamr only stops a process it owns.
