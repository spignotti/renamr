# Feature: Vision Rework & Per-Inbox Config

## Context

- **Project**: renamr — https://github.com/spignotti/renamr
- **Category**: oss
- **Branch**: `feat/vision-rework`
- **Affected Modules**: `extractor`, `config`, `processor`, `cli (init template)`, `README`

## Goal

Simplify content extraction to a hybrid pipeline (text-first, vision-fallback for scans), support Ollama as a first-class local backend via LiteLLM, and allow per-inbox configuration of template, language, and prompt. Result: simpler codebase, no separate OCR model, full local-or-cloud flexibility.

## Changes

### API / Endpoints

- Keine neuen CLI-Commands.
- `renamr init` generiert ein aktualisiertes Config-Template mit `[[inbox]]`-Syntax und Ollama-Beispiel.

### Data Model / Config

Aktuelles Flat-Format:

```toml
inbox_paths = ["/path/to/folder"]
language = "en"
filename_template = "{date}_{sender}_{subject}"

[llm]
model = "gpt-4o-mini"
```

Neues Format mit per-inbox Overrides:

```toml
[llm]
model = "ollama/gemma4:e2b"
api_base = "http://localhost:11434"
temperature = 0.2
max_retries = 2
timeout = 60

[[inbox]]
path = "/Users/silas/Documents/Rechnungen"
filename_template = "{date}_{sender}_{subject}"
language = "de"

[[inbox]]
path = "/Users/silas/Documents/Scans"
filename_template = "{date}_{subject}"
language = "en"
rename_prompt = "Extract only date and subject. Ignore sender."

[compress]
enabled = false
dpi = 150
jpeg_quality = 80

[logging]
level = "INFO"
json_logs = false
```

- `[[inbox]]` ersetzt `inbox_paths`-Array. Jede Sektion ist ein Ordner.
- Pro Inbox optional: `filename_template`, `language`, `rename_prompt`. Fehlende Felder fallen auf globale Defaults zurück.
- Globale Defaults können weiterhin auf Top-Level definiert werden (`language`, `filename_template`, `rename_prompt`) — werden von Inbox-Werten überschrieben.
- `file_extensions` und `recursive` bleiben global (kein Per-Inbox-Override — YAGNI).
- Backwards Compatibility: altes `inbox_paths`-Format wird erkannt und mit Deprecation-Warning weiterhin akzeptiert (1 Major Version lang).

### Logic / Core

**Extractor — Hybrid-Pfad:**

- Kein separater OCR-Code mehr. Ein Modell, zwei Eingabeformen.
- PDF → `pypdf` Textextraktion. Text vorhanden: direkt ans LLM. Kein Text (Scan): `pymupdf` rendert zu Image → ans LLM.
- Native Bilddateien (JPEG, PNG): Pillow → Base64 → LLM wie bisher.
- Entscheidung Text vs. Image ist intern im Extractor, nicht konfigurierbar.
- `pypdf` bleibt als Dependency (günstiger bei Cloud, schneller lokal für Text-PDFs).
- `pymupdf` bleibt für Scan-Rendering und Compress.

**Config-Layer:**

- Pydantic-Modell wird umgebaut: `InboxConfig` als eigener Typ mit optionalen Override-Feldern.
- `AppConfig` hält eine Liste von `InboxConfig` plus globale Defaults.
- Merge-Logik: Effektive Config pro Inbox = globale Defaults + Inbox-Overrides.

**Processor:**

- Iteriert über `List[InboxConfig]` statt `List[str]`.
- Übergibt effektive Inbox-Config an Extractor und Renamer.

### Frontend

- Kein Frontend. CLI-Output bleibt unverändert.

### README

- Neue Struktur: Quick Start → Configuration → Ollama Setup → Cloud Providers → CLI Reference → Privacy
- Neuer Abschnitt **Ollama Setup** mit minimalem Copy-Paste-Beispiel (`gemma4:e2b`).
- Config-Referenz auf neues `[[inbox]]`-Format aktualisieren.
- Privacy-Callout: bei lokalem Modell keine externen API-Calls.

## Implementation Plan

- [ ]  Extractor auf Hybrid-Pfad umbauen: Text-first (pypdf), Vision-Fallback (pymupdf) fuer Scans
- [ ]  Pydantic-Config-Modell umbauen: `InboxConfig` + Merge-Logik
- [ ]  `renamr init`-Template auf `[[inbox]]`-Format aktualisieren + Ollama-Beispiel
- [ ]  Processor auf `List[InboxConfig]` umstellen
- [ ]  Backwards-Compatibility für `inbox_paths` mit Deprecation-Warning
- [ ]  README neu strukturieren (Ollama-Sektion, Config-Referenz, Privacy-Update)
- [ ]  Tests: Extractor (Hybrid-Pfad), Config-Merge-Logik, Backwards-Compat
- [ ]  Changelog + Version bump (Minor: 2.x → 2.(x+1))

## Constraints

- Hybrid-Pfad: `pypdf` bleibt Dependency. Kein Breaking Change.
- Cloud-Modelle: Text-PDFs laufen günstig über Text-Input; nur Scans verursachen Vision-Kosten.
- Ollama muss lokal laufen — kein Zero-Dependency-Setup mehr, wenn lokale Modelle genutzt werden.
- `pymupdf` bleibt Pflicht-Dependency (Rendering + Compress).

## Non-Goals

- Per-Inbox LLM-Override (anderes Modell pro Ordner)
- Neue CLI-Commands
- Interaktiver Setup-Wizard
- Automatischer Ollama-Download / -Start
