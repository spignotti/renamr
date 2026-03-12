# File Renamer

## Identity
- **What**: CLI tool that renames local files using AI-powered metadata extraction
- **Why**: Incoming documents (PDFs, scans, images) have meaningless filenames — this tool renames them to `YYMMDD_Sender_Subject.ext` based on content analysis
- **Type**: cli
- **Python**: 3.12

## Architecture
- **Framework**: Typer
- **LLM**: LiteLLM (provider-agnostic)
- **Dependencies**:
  - `litellm`: unified LLM interface (supports OpenAI, OpenRouter, Anthropic, etc. — provider switch is config-only)
  - `pypdf`: PDF text extraction
  - `pymupdf` (fitz): PDF-to-image rendering for scanned/image-only PDFs
  - `pillow`: image encoding for vision API
  - `pydantic`: config validation
  - `structlog`: structured logging
- **Secrets**: `OPENAI_API_KEY` (or `OPENROUTER_API_KEY` later — LiteLLM resolves by model prefix)
- **Config**: `config.toml` (TOML) — inbox path, file extensions, LLM model + optional api_base, logging
### LLM Provider Strategy
Use `litellm.completion()` instead of the OpenAI client directly.
Current default: `model = "gpt-4o-mini"` with `OPENAI_API_KEY`.
Later switch to OpenRouter: change model to `"openrouter/anthropic/claude-sonnet-4"` + set `api_base` in config. No code changes needed.
LiteLLM supports vision and JSON mode — both required for the renamer.

## Source Reference
Extract and modernize from local `automation` repo:
- `src/automation_platform/processors/ai_renamer.py` → core rename pipeline
- `src/automation_platform/integrations/openai_client.py` → OpenAI prompt + metadata parsing
- `src/automation_platform/integrations/file_utils.py` → filename building, sanitization, move
- `src/automation_platform/integrations/image_utils.py` → base64 encoding, PDF page rendering
- `src/automation_platform/config/models.py` → config models (strip platform-specific parts)

Path: "~/Documents/projects/dev/automation"


**What to drop:**
- Event-driven architecture (`Processor`, `FileAddedEvent`, `ProcessingResult`)
- Async everywhere — replace with sync (CLI doesn't need async)
- `ProcessedFilesStore` (deduplication) — unnecessary for CLI
- `RetryQueue` — unnecessary for CLI
- All Instagram/Literature references
- `core/error_handling.py` error categorization — simplify to try/except + log

**What to improve:**
- Sync pipeline: scan folder → filter by extension → extract preview → call LLM → build filename → rename (or dry-run)
- First-class `--dry-run` flag: show planned renames without executing
- Recursive folder scanning with `--recursive` flag
- Summary report after each run (renamed, skipped, failed)
- Simpler flat config (no nested platform config)
- Better CLI UX: colored output, progress indicator for multi-file runs
- **iCloud file handling**: detect `.icloud` stub files, trigger download via `brctl download <path>`, poll until materialized (with timeout), skip + log on timeout
- **PDF compression**: `--compress` flag to reduce file size of scanned PDFs (re-render pages via pymupdf at configurable DPI + JPEG quality). No extra dependency needed.
- **Undo log**: each run writes a JSON log (`data/undo.json`) mapping `old_path → new_path`. `file-renamer undo` reverses the last run. Simple, safe, zero overhead.

## Project Structure
file-renamer/
├── src/
│   └── file_renamer/
│       ├── __init__.py
│       ├── cli.py            # Typer CLI (entry point)
│       ├── config.py         # Pydantic config models + TOML loader
│       ├── renamer.py        # Core rename pipeline (scan → extract → rename)
│       ├── metadata.py       # LiteLLM client + system prompt + response parsing
│       ├── preview.py        # Content extraction (PDF text, image encoding, PDF render)
│       └── files.py          # Filename building, sanitization, move/rename
├── tests/
├── data/                     # Runtime data (undo log, etc.)
├── config.toml.example
├── .env.example
├── LICENSE                   # MIT
├── pyproject.toml
├── .github/
│   └── workflows/
│       └── ci.yml            # Lint + typecheck + test
├── noxfile.py
├── AGENTS.md
└── README.md

## Objectives
### MVP (v0.1)
- [ ] Rename PDFs in a folder using AI metadata extraction (text + vision fallback)
- [ ] Rename image files (JPG, PNG, etc.) using vision API
- [ ] Rename plain text files using text preview
- [ ] `--dry-run` flag showing planned renames without executing
- [ ] Configurable inbox path and file extensions via `config.toml`
- [ ] Conflict handling: append `_2`, `_3` etc. for duplicate filenames
- [ ] Summary report after run (renamed / skipped / failed)
- [ ] iCloud stub detection + download trigger (`brctl download`) with polling/timeout
- [ ] `--compress` flag for PDF file size reduction (pymupdf re-render)
- [ ] Undo log (`data/undo.json`) + `file-renamer undo` command
- [ ] OSS setup: MIT license, CI workflow, README with badges, `.github/workflows/ci.yml`

### Nice-to-have (only if time)
- [ ] `--recursive` flag for nested folder scanning
- [ ] `--watch` mode (filesystem watcher on folder)

### Non-Goals
- No Notion integration
- No async / background daemon
- No deduplication tracking
- No retry queue
- No Instagram / literature / content pipeline features

## Setup
- **Git Remote**: https://github.com/silas-workspace/file-renamer.git
- **Visibility**: Public (OSS, MIT license)
- **CI**: GitHub Actions (uv sync → nox: lint, typecheck, test)
