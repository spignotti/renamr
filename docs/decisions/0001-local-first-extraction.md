# Decision 0001: Local-first extraction with explicit external LLM use

`status`: accepted
`supersedes`: none
`superseded_by`: none

## Context

Renaming requires reading user file content, and the metadata step needs an LLM. Sending every file to an external provider by default maximizes exposure of potentially sensitive documents.

## Options considered

- External-first: send all extracted content to the configured provider — simplest pipeline, highest data exposure.
- Local-first with vision fallback: extract locally where possible, call a model only for metadata parsing and failed extractions — more pipeline stages, minimal external exposure.

## Decision

Local-first: content is extracted on-device first, with a configured vision model only as fallback. Provider choice stays config-only, and the README discloses that file content reaches an LLM provider. Only filenames change; undo covers the most recent successful run.

## Consequences

- Most image and scan inputs never leave the machine before the metadata step.
- Extraction has more stages to maintain than a single provider call.
- Users opting into cloud models accept explicit, documented external use.

## Revisit trigger

A new local extraction capability that removes the metadata-step provider call, or a privacy requirement that forbids any external content transfer.
