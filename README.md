# renamr

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
[![PyPI](https://img.shields.io/pypi/v/renamr.svg)](https://pypi.org/project/renamr/)
[![CI](https://github.com/spignotti/renamr/actions/workflows/ci.yml/badge.svg)](https://github.com/spignotti/renamr/actions/workflows/ci.yml)

AI-powered CLI that renames files based on their content.

## What it does

Scanned documents, downloads, and exported files often arrive with useless names like `scan_001.pdf` or `IMG_5847.jpg`. renamr reads each file — extracting text from PDFs, rendering pages as images for vision models, or encoding photos directly — sends a preview to an LLM, and renames the file to a structured format based on the content it actually finds.

```
scan_001.pdf          ->  240115_ACME_Rechnung.pdf
IMG_5847.jpg          ->  241203_DeutschePost_Zustellbenachrichtigung.jpg
invoice_download.pdf  ->  250110_Amazon_Bestellbestaetigung.pdf
```

Only the filename changes. Files are never modified.

## Features

- Content-aware renaming via any LiteLLM-supported provider (OpenAI, OpenRouter, Anthropic, local models)
- PDF text extraction for text-based documents
- Vision model support for scanned PDFs and image files
- iCloud evicted file handling — triggers download via `brctl` before processing (macOS)
- Dry-run mode to preview renames without touching files
- Undo the last run with a single command
- Configurable output template (`{date}_{sender}_{subject}`), file extensions, and system prompt
- Optional in-place PDF compression after renaming

## Installation

Requires Python 3.12 or newer.

```bash
pip install renamr
```

```bash
uv tool install renamr
```

## Quick Start

```bash
# Create config.toml and data/ in the current directory
renamr init

# Set your API key
export OPENAI_API_KEY="your-key"

# Preview renames without touching any files
renamr run --dry-run

# Rename files
renamr run

# Undo the last run
renamr undo
```

Override the inbox without editing config:

```bash
renamr run --inbox ~/Documents/inbox --dry-run
```

## Configuration

`renamr init` creates a `config.toml` in the current directory. The full set of options:

```toml
inbox_path = "."
file_extensions = [".pdf", ".jpg", ".jpeg", ".png", ".txt"]
recursive = false
filename_template = "{date}_{sender}_{subject}"
# rename_prompt = "..."  # override the system prompt sent to the model

[llm]
model = "gpt-4o-mini"
api_base = ""        # leave empty for direct OpenAI; set for OpenRouter or local endpoints
temperature = 0.2
max_retries = 2
timeout = 60

[compress]
enabled = false      # re-render PDFs at lower DPI after renaming
dpi = 150
jpeg_quality = 80

[logging]
level = "INFO"
json_logs = false
```

`filename_template` supports three placeholders: `{date}`, `{sender}`, `{subject}`. The date is extracted from document content when available, falling back to the file's creation timestamp.

`data/undo.json` is stored relative to the config file. Always run `renamr run` and `renamr undo` with the same `--config` path, or from the same directory when using the default.

**Switching providers.** Change `model` and set `api_base`. For OpenRouter:

```toml
[llm]
model = "openrouter/openai/gpt-4o-mini"
api_base = "https://openrouter.ai/api/v1"
```

Then set `OPENROUTER_API_KEY` instead of `OPENAI_API_KEY`. Any provider supported by LiteLLM works without code changes.

**Customizing the prompt.** The default system prompt extracts sender, subject, and date from documents and handles German and English. To override, uncomment `rename_prompt` in `config.toml` and replace it with your own. The full default is in `src/renamr/models.py`.

## Privacy & Security

> [!WARNING]
> **renamr sends file content to an external LLM API.**
>
> Depending on your configuration, this includes:
> - Extracted text from PDF and `.txt` files
> - Rendered page images from scanned PDFs
> - Raw image data from `.jpg`, `.png`, and other supported image files
> - Original filenames and file timestamps
>
> This data is transmitted to your configured LLM provider and may be processed on remote servers. **Do not run renamr on sensitive or confidential files unless you have reviewed and accepted your provider's data handling policy.**
>
> Run `renamr run --dry-run` first to confirm which files will be processed.

Additional notes:

- Always use an `https://` endpoint for `api_base`. An `http://` URL sends file content unencrypted.
- Keep `data/undo.json` private on shared systems — it contains the file paths from the last run.
- Avoid sharing verbose log output publicly; failed auth responses may include API key fragments.

## Maintenance

This tool is maintained for personal use and published as-is. Bug reports welcome via Issues. No guaranteed response time. PRs accepted if they align with the project's scope.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup.

## License

MIT
