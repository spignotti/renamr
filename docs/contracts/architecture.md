# Architecture Contract

Normative component and runtime decisions. Keep to accepted constraints and invariants, not file trees or dependency versions.

## System boundary

renamr is a local, synchronous CLI that renames user files from AI-extracted metadata. It deliberately is not a server, background service, database-backed application, or hosted API.

## Components and responsibilities

| Component | Responsibility | Owns |
|---|---|---|
| Content extraction | Produce text from PDFs, images, and scans, local-first with a configured vision fallback. | Extraction cascade policy (reference: DEC-0001) |
| Metadata parsing | Turn extracted text into structured rename metadata through the configured provider. | Prompt shape and metadata schema |
| File operations | Build filenames, rename files, and record undo state. | Rename safety invariants |
| CLI wiring | Parse commands, load per-inbox configuration, and orchestrate the pipeline. | Config schema and command surface |

## Key connections / data flow

Inbox files flow one way: scan, extract content, parse metadata, build filename, rename. Undo state is written beside the config file. Provider calls cross the only external boundary, using the model and endpoint from configuration.

## Runtime invariants

- Only the filename changes; file bytes are never modified (reference: DEC-0001).
- Undo restores the most recent successful run only.
- Runtime data stays out of versioned outputs.
- Provider selection is configuration, never code.

## External services and deployment

Any LiteLLM-supported provider, including a local instance, selected entirely through configuration. Distribution is a published Python package; there is no deployment target.
