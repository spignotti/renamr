# renamr

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
[![PyPI](https://img.shields.io/pypi/v/renamr.svg)](https://pypi.org/project/renamr/)
[![CI](https://github.com/spignotti/renamr/actions/workflows/ci.yml/badge.svg)](https://github.com/spignotti/renamr/actions/workflows/ci.yml)

AI-powered CLI that renames files based on their content.

## What it does

Scanned documents, downloads, and exported files often arrive with useless names like `scan_001.pdf` or `IMG_5847.jpg`. renamr reads each file — extracting text from PDFs, rendering pages as images for vision models, or encoding photos directly — sends a preview to an LLM, and renames the file to a structured format based on the content it actually finds.

```
scan_001.pdf          ->  240115_ACME_Invoice.pdf
IMG_5847.jpg          ->  241203_PostOffice_DeliveryNotice.jpg
invoice_download.pdf  ->  250110_Amazon_OrderConfirmation.pdf
```

Only the filename changes. Files are never modified.

## Features

- Content-aware renaming via any LiteLLM-supported provider (OpenAI, OpenRouter, Anthropic, **Ollama**, local models)
- Hybrid extraction: text-first for regular PDFs, vision fallback for scans
- Per-inbox configuration: different templates, languages, and prompts per folder
- iCloud evicted file handling — auto-downloads stubs via `brctl` before processing (macOS only)
- Multi-inbox support — configure one or more folders in a single config
- Configurable output language — extracted metadata returned in any language
- Dry-run mode to preview renames without touching files
- Undo the last run with a single command
- Optional in-place PDF compression after renaming

## Quick Start

```bash
# One-time global install
uv tool install renamr   # or: pip install renamr

# First-run setup — creates ~/.config/renamr/config.toml
renamr init

# Set your API key (for cloud providers)
export OPENAI_API_KEY="your-key"

# Preview renames
renamr run --dry-run

# Rename files
renamr run

# Undo last run
renamr undo
```

Override the inbox without editing config:

```bash
renamr run --inbox ~/Documents/inbox --dry-run
```

## Configuration

`renamr init` creates `~/.config/renamr/config.toml` by default. On Linux, `XDG_CONFIG_HOME`
is respected, so the actual path becomes `$XDG_CONFIG_HOME/renamr/config.toml` when set.

### New `[[inbox]]` Format

The config uses TOML array-of-tables syntax for per-inbox settings:

```toml
# Global defaults (used when not overridden per inbox)
language = "en"
filename_template = "{date}_{sender}_{subject}"

[[inbox]]
path = "/Users/you/Documents/Invoices"

[[inbox]]
path = "/Users/you/Documents/Scans"
filename_template = "{date}_{subject}"
language = "de"
rename_prompt = "Extract only date and subject. Ignore sender."

[llm]
model = "gpt-4o-mini"

[compress]
enabled = false
dpi = 150
jpeg_quality = 80

[logging]
level = "WARNING"
json_logs = false
```

Each `[[inbox]]` section represents one folder. Missing fields fall back to global defaults:

- `filename_template` — optional override
- `language` — optional override
- `rename_prompt` — optional override

`file_extensions` and `recursive` remain global (no per-inbox override).

### Legacy Format

The old `inbox_paths = ["/path/one", "/path/two"]` format is still supported but deprecated:

```toml
# DEPRECATED - still works but will be removed in a future version
inbox_paths = ["/path/to/folder"]
language = "en"
filename_template = "{date}_{sender}_{subject}"
```

## Ollama Setup

Run renamr completely offline with a local Ollama instance.

**1. Install Ollama**

```bash
# macOS
brew install ollama

# Or download from https://ollama.com
```

**2. Pull a vision-capable model**

```bash
ollama pull gemma4:e2b
```

**3. Configure renamr**

```toml
[llm]
model = "ollama/gemma4:e2b"
api_base = "http://localhost:11434"
temperature = 0.2
```

That's it. Files are processed locally with no external API calls.

> **Note:** Ollama must be running (`ollama serve`) before you run renamr.

## Cloud Providers

Change `model` and set `api_base`. For OpenRouter:

```toml
[llm]
model = "openrouter/openai/gpt-4o-mini"
api_base = "https://openrouter.ai/api/v1"
```

Then set `OPENROUTER_API_KEY` instead of `OPENAI_API_KEY`. Any provider supported by LiteLLM works without code changes.

### Custom Prompt

The default system prompt extracts sender, subject, and date from documents and handles German and English. To override, add a `rename_prompt` field to your config (globally or per-inbox):

```toml
[[inbox]]
path = "/Users/you/Documents/Receipts"
rename_prompt = """
Extract only the total amount and date from this receipt.
Return JSON: {"sender":"Store Name","subject":"Receipt $AMOUNT","date":"YYYY-MM-DD"}
"""
```

The full default prompt is in `src/renamr/models.py`.

## CLI Reference

```
renamr init [--config PATH]     Create a config file interactively
renamr run [--config PATH]      Process files and rename them
  --dry-run                     Preview without renaming
  --inbox PATH                  Override inbox folder
  --recursive / --no-recursive  Override recursive setting
  --compress / --no-compress    Override compression setting
  --verbose                     Enable debug logging
renamr undo [--config PATH]     Undo the last successful run
renamr version                  Print version
```

`undo.json` is stored next to the config file. With the default setup, that means
`~/.config/renamr/undo.json`.

## Privacy & Security

> [!WARNING]
> **renamr sends file content to an LLM API.**
>
> Depending on your configuration, this includes:
> - Extracted text from PDF and `.txt` files
> - Rendered page images from scanned PDFs
> - Raw image data from `.jpg`, `.png`, and other supported image files
> - Original filenames and file timestamps
>
> When using **cloud providers**, this data is transmitted to remote servers. **Do not run renamr on sensitive or confidential files unless you have reviewed and accepted your provider's data handling policy.**

> [!NOTE]
> **Local models (Ollama) process files entirely on your machine.**
>
> When configured with a local Ollama instance, no file content leaves your computer. This is the recommended setup for sensitive documents.

Additional notes:

- Always use an `https://` endpoint for `api_base`. An `http://` URL sends file content unencrypted.
- Keep `~/.config/renamr/undo.json` private on shared systems — it contains the file paths from the last run.
- Avoid sharing verbose log output publicly; failed auth responses may include API key fragments.

## Maintenance

This tool is maintained for personal use and published as-is. Bug reports welcome via Issues. No guaranteed response time. PRs accepted if they align with the project's scope.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup.

## License

MIT
