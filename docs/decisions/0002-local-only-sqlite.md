# Decision 0002: Local-only inference and SQLite-owned local state

`status`: accepted
`supersedes`: 0001
`superseded_by`: none

## Context

The v1 tool used LiteLLM for provider-agnostic cloud or local inference and kept
active behavior in TOML. That made cloud use easy to configure accidentally,
could not provide a durable catalog, and had no crash-safe mutation journal.

## Decision

- The filesystem stays the document store. Original bytes are never rewritten.
- SQLite in a private state directory owns the catalog, settings, roots, rules,
  decisions, contracts, and operation history. A minimal bootstrap TOML only
  points at that state directory.
- Inference is local Ollama only, over a loopback endpoint, with strict local
  model verification and no downloads from inside processing.
- Cloud providers, LiteLLM, remote endpoints, PDF compression, automatic
  deletion, vector search, agent tools, and unattended global restructuring are
  outside the product.
- Mutations go through a journaled plan, a cross-process writer lock, and
  no-overwrite publication. The local model can propose but never execute or
  confirm an action.
- Read-only MCP export is opt-in per contract alias and enumerated field.

## Consequences

- Users must install a local model explicitly (`ollama pull`).
- SQLite is not encrypted by default; OS-level disk encryption and backups remain
  the user's responsibility.
- The document catalog contains private text and facts; diagnostic logs stay
  sanitized and separate.
- Cloud-hosted MCP clients can receive every permitted response. The local
  inference policy does not make those responses private from that client.

## Revisit trigger

An explicitly approved local-only encryption layer, a separately approved remote
inference mode, or an event-driven watcher that replaces scheduled reconciliation
without weakening the writer/journal invariants.
