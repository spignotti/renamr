# Renamr
[![CI](https://github.com/spignotti/renamr/actions/workflows/ci.yml/badge.svg)](https://github.com/spignotti/renamr/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/renamr.svg)](https://pypi.org/project/renamr/)

Renamr is a Python CLI that scans a folder of local documents, extracts lightweight previews, sends that context to an LLM through LiteLLM, and renames files into consistent metadata-based filenames such as `YYMMDD_Sender_Subject.pdf`.

## Privacy & Security Warning

This tool sends file contents to an external LLM API.

That can include:
- text previews extracted from `.txt` and `.pdf` files
- rendered document images for image files and image-only PDFs
- original filenames and file timestamps

Review what is inside your inbox folder before running the tool. Start with `--dry-run` so you can inspect planned renames before any files move. API keys stay on your machine in environment variables, but file contents are sent to the configured provider's API. The tool includes original filenames in the LLM prompt, so avoid running it on directories containing untrusted filenames. Keep `data/undo.json` private on shared systems. A user with write access to that file could cause `renamr undo` to move files to unexpected locations. Avoid sharing verbose log output publicly because failed auth responses may include API key fragments.

## Quick Start

Install with either tool:

```bash
uv tool install renamr
```

or:

```bash
pipx install renamr
```

Initialize local config and runtime data:

```bash
renamr init
```

Preview changes without renaming anything:

```bash
renamr run --dry-run
```

## Configuration

Renamr reads settings from `config.toml`.

Key fields:
- `inbox_path`: folder to scan
- `file_extensions`: allowed file types
- `recursive`: recurse into subfolders
- `filename_template`: output pattern such as `{date}_{sender}_{subject}`
- `rename_prompt`: system prompt sent to the model
- `[llm]`: model name, optional `api_base`, temperature, retries, timeout (always use an `https://` endpoint; `http://` sends file content unencrypted)
- `[compress]`: optional PDF recompression settings
- `[logging]`: log level and JSON output toggle

`rename_prompt` is intentionally configurable. If your documents are domain-specific, copy the full default prompt from `src/renamr/models.py` and adapt it for your own naming rules.

API keys come from environment variables. With the default OpenAI models, set `OPENAI_API_KEY`. For OpenRouter models, set `OPENROUTER_API_KEY` and use a provider-prefixed model name.

`config.toml` is selected with `--config` and defaults to the current working directory. `data/undo.json` is stored relative to the selected config file, so run `renamr run` and `renamr undo` against the same config path.

## Commands

### `renamr run`

Scan the inbox, extract metadata, and rename files.

Examples:

```bash
renamr run --dry-run
renamr run --inbox ~/Downloads/inbox
renamr run --recursive --compress
```

### `renamr undo`

Undo the most recent successful rename run using `data/undo.json`.

```bash
renamr undo
```

### `renamr init`

Create `config.toml` from `config.toml.example` and ensure `data/` exists.

```bash
renamr init
```

### `renamr version`

Print the installed version.

```bash
renamr version
```

## Provider Examples

Default OpenAI setup:

```bash
export OPENAI_API_KEY="your-key"
renamr run --dry-run
```

OpenRouter setup:

```bash
export OPENROUTER_API_KEY="your-key"
```

```toml
[llm]
model = "openrouter/openai/gpt-4o-mini"
api_base = "https://openrouter.ai/api/v1"
```

## Development

```bash
uv sync
uv run nox
uv run renamr --help
```

Main modules:
- `src/renamr/preview.py`: text extraction, PDF rendering, image encoding, PDF compression
- `src/renamr/metadata.py`: LiteLLM request + metadata/date parsing
- `src/renamr/files.py`: filename building, conflict handling, iCloud helpers
- `src/renamr/renamer.py`: scan/process/undo pipeline
- `src/renamr/cli.py`: Typer commands

## License

MIT
