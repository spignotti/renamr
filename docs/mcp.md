# Read-only MCP surface

renamr exposes a deliberately tiny MCP server over stdio. There is no HTTP
listener, resource surface, SQL tool, filesystem tool, full-text search, excerpt,
summary, or mutation.

## Start

```bash
renamr mcp serve --state-dir "$HOME/Library/Application Support/renamr"
```

An MCP host normally launches this command. Use an explicit private state
directory or a v2 bootstrap via `--config`. The server never creates state.

## Tools

| Tool | Returns |
|---|---|
| `list_approved_contracts` | Up to 50 aliases that have at least one live grant. No contract ids. |
| `get_contract_facts(alias)` | Only granted fields for that alias, normalized dates/numbers/booleans/enums/labels, with a verification state. |

Denied and unknown aliases return the same `available: false` shape. Discovery
never reveals a contract without a grant. Evidence quotes, paths, revision ids,
full text, and free-form model strings are never returned.

## Permissions

Default deny. Grant enumerated fields for one safe-aliased contract:

```bash
renamr contracts alias <contract-id> <safe-alias>
renamr permissions grant --contract-id <contract-id> \
  --field provider --field end_date --yes
renamr permissions list
renamr permissions revoke --contract-id <contract-id> --field provider
```

Allowed fields: `provider`, `contract_type`, `start_date`,
`minimum_term_months`, `renewal`, `notice_period_days`, `end_date`,
`cancellation_confirmed`, `cost_amount`, `cost_currency`, `cost_interval`,
`status`, `minimum_end_date`, `notice_deadline`.

Private contract/reference numbers and the derived `entity_id` are local routing
inputs only; they are intentionally absent from this allowlist.

Unknown or conflicting facts may return only their verification state without a
value. Permission changes are read from SQLite on every request; revocation takes
effect on the next request without restarting the server.

## Disclosure

> [!WARNING]
> A cloud-hosted MCP client or model can receive every permitted response. The
> local-only inference policy does not make those responses private from that
> client. This interface also cannot protect against a client that independently
> has shell or filesystem access; such a client can read the same private state
> directly.

The local CLI remains the full-evidence surface. MCP intentionally returns less.
