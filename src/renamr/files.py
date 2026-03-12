"""File operation helpers for renaming and iCloud handling."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path

MAX_FILENAME_LENGTH = 200
UNSAFE_CHARS_PATTERN = re.compile(r'[\\/:*?"<>|]')
WHITESPACE_PATTERN = re.compile(r"\s+")


def sanitize_component(value: str) -> str:
    """Sanitize one filename component for safe local filesystem use."""
    cleaned = UNSAFE_CHARS_PATTERN.sub("", value).replace("_", " ")
    cleaned = WHITESPACE_PATTERN.sub(" ", cleaned).strip(" .")
    return cleaned or "Unknown"


def build_filename(
    date_prefix: str,
    sender: str,
    subject: str,
    extension: str,
    template: str,
    filename_format: str,
) -> str:
    """Build a safe filename from metadata and a configurable template."""
    safe_template = (
        _drop_sender_placeholder(template)
        if filename_format == "date_subject"
        else template
    )
    safe_sender = sanitize_component(sender)
    safe_subject = sanitize_component(subject)
    base_name = _format_base_name(safe_template, date_prefix, safe_sender, safe_subject)
    allowed_length = MAX_FILENAME_LENGTH - len(extension)
    if len(base_name) <= allowed_length:
        return f"{base_name}{extension}"
    trim_by = len(base_name) - allowed_length
    if trim_by < len(safe_subject):
        truncated_subject = safe_subject[:-trim_by].rstrip(" _.-")
    else:
        truncated_subject = "D"
    base_name = _format_base_name(safe_template, date_prefix, safe_sender, truncated_subject)
    return f"{base_name[:allowed_length].rstrip(' _.-')}{extension}"


def resolve_conflict(dest: Path) -> Path:
    """Return a non-conflicting destination path by appending a numeric suffix."""
    if not dest.exists():
        return dest
    counter = 2
    while True:
        candidate = dest.with_name(f"{dest.stem}_{counter}{dest.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def rename_file(src: Path, dest_dir: Path, filename: str) -> Path:
    """Move a file into the destination directory with conflict handling."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = resolve_conflict(dest_dir / filename)
    shutil.move(str(src), str(destination))
    return destination


def select_date_prefix(document_date: date | None, file_created: date) -> str:
    """Prefer document date and fall back to the file creation date."""
    return (document_date or file_created).strftime("%y%m%d")


def is_icloud_stub(filepath: Path) -> bool:
    """Return True when a path matches the macOS iCloud stub naming pattern."""
    return filepath.name.startswith(".") and filepath.name.endswith(".icloud")


def download_icloud_file(stub_path: Path, timeout: int = 30) -> Path | None:
    """Ask iCloud to download the real file and wait until it appears locally."""
    real_path = resolve_icloud_path(stub_path)
    try:
        subprocess.run(["brctl", "download", str(real_path)], check=False, capture_output=True)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if real_path.exists():
            return real_path
        time.sleep(0.5)
    return None


def resolve_icloud_path(stub_path: Path) -> Path:
    """Convert a `.name.ext.icloud` stub path into the real file path."""
    name = stub_path.name
    if name.startswith("."):
        name = name[1:]
    if name.endswith(".icloud"):
        name = name[: -len(".icloud")]
    return stub_path.with_name(name)


def _drop_sender_placeholder(template: str) -> str:
    """Remove sender placeholders from a template when sender is not useful."""
    return (
        template.replace("_{sender}", "")
        .replace("{sender}_", "")
        .replace("{sender}", "")
    )


def _format_base_name(template: str, date_prefix: str, sender: str, subject: str) -> str:
    """Render a template and normalize repeated separators."""
    rendered = template.format(date=date_prefix, sender=sender, subject=subject)
    rendered = re.sub(r"_+", "_", rendered)
    return rendered.strip(" _.-") or "Unknown"
