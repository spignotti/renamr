# Renamr
[![CI](https://github.com/spignotti/renamr/actions/workflows/ci.yml/badge.svg)](https://github.com/spignotti/renamr/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/renamr.svg)](https://pypi.org/project/renamr/)

CLI tool that renames local files using AI-powered metadata extraction.

## Overview

This repository contains the initial scaffold for a Typer-based CLI that will:

- scan a folder for supported files
- extract preview data from documents and images
- call an LLM through LiteLLM
- build safe filenames in the form `YYMMDD_Sender_Subject.ext`
- support dry runs, undo logs, and optional PDF compression

The scaffold intentionally stops at project setup. Product logic belongs in later `/plan` and `/task` work.

## Tech Stack

- Python 3.12
- Typer CLI
- LiteLLM for provider-agnostic model calls
- Pydantic and pydantic-settings for config and validation
- PyPDF, PyMuPDF, and Pillow for document preview handling
- Structlog for logging
- Ruff, Pyright, Pytest, and Nox for quality checks

## Project Layout

```text
src/renamr/
tests/
docs/decisions/
.github/workflows/
data/
.pi/
```

## Development

```bash
uv sync
uv run nox
uv run renamr --help
```

## Configuration

- Copy `.env.example` to `.env` and set provider keys.
- Copy `config.toml.example` to `config.toml` and adjust local settings.

## Status

Scaffold only. No rename pipeline has been implemented yet.
