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

- Take the global writer lock non-blockingly before database initialization and
  hold the same lock for the whole tick. A concurrent worker prints a sanitized
  `busy` result promptly instead of blocking behind another writer or racing a
  migration.
- Reconcile non-terminal journal items before new work.
- Scan registered roots with a cheap stat fast path. Unchanged content causes no
  hash, OCR, or model call.
- Budget defaults per tick: 4 documents, 4 model calls, 50 pages, about 90
  seconds. Settings are database-owned (`service.max_documents`,
  `service.max_model_calls`, `service.max_pages`, `service.max_seconds`,
  `service.interval_s`).
- Model-call and time budgets are passed into the feature chunk loop, so one
  long document cannot make ten requests when one call remains. The remaining
  time is also the per-request HTTP timeout (including registry/model preflight),
  so a service budget of 90 seconds cannot leave a 120-second socket wait. A
  deferral is recorded as `feature_deferred` with a manual retry path; it is
  never marked complete, permanently failed, or routable.
- A revision is routing-eligible only when its feature stage is complete, not
  failed/deferred/blocked, and its cache key is current. Older facts remain in the
  catalog for private inspection but cannot drive automatic routing or a pending
  plan after a partial/failed refresh.
- A changed file whose page count does not fit the remaining page budget is
  deferred with a `scan_budget_deferred` review item and manual retry path. It is
  never marked complete.
- Deferred large files do not stop later roots in the same tick. The tick scans
  every root while budget remains; repeated deferrals stay actionable and do not
  starve other inboxes.
- Feature work starts/reuses an eligible local server only when there is actual
  work. Startup, verification, and feature work share one owned-process cleanup,
  so a verification or extraction failure still stops only a server this app
  started. Role-specific keep-alive avoids unnecessary resident models; requests
  are sequential.
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
