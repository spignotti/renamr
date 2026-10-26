# Decision 0001: Local-first extraction with explicit external LLM use

`status`: historical / superseded by 0002
`supersedes`: none
`superseded_by`: 0002

## Context

Renaming requires reading user file content, and the original metadata step used
an LLM. Sending every file to an external provider by default maximized exposure
of potentially sensitive documents.

## Decision (historical)

Local-first extraction with a configurable external provider for metadata, and a
vision fallback. Only filenames changed; undo covered the most recent run.

## Why it was superseded

Version 2 requires local-only inference, a SQLite-owned catalog, journaled
mutation, and opt-in read-only sharing. External providers, LiteLLM, and PDF
compression were removed. See decision 0002.
