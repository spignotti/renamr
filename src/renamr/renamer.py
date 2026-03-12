"""Core rename pipeline for renamr."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from renamr.files import (
    build_filename,
    download_icloud_file,
    is_icloud_stub,
    rename_file,
    resolve_icloud_path,
    select_date_prefix,
)
from renamr.metadata import extract_metadata
from renamr.models import AppConfig
from renamr.preview import (
    compress_pdf,
    encode_image_base64,
    extract_text_preview,
    is_image_file,
    render_pdf_page,
)

UNDO_FILENAME = "undo.json"


@dataclass(frozen=True)
class RenameResult:
    """Result for processing a single file."""

    old_path: Path
    new_path: Path | None
    new_name: str | None
    status: str
    error: str | None = None


@dataclass(frozen=True)
class RunSummary:
    """Summary for one rename run."""

    results: list[RenameResult]

    @property
    def renamed(self) -> int:
        return sum(result.status == "renamed" for result in self.results)

    @property
    def skipped(self) -> int:
        return sum(result.status == "skipped" for result in self.results)

    @property
    def failed(self) -> int:
        return sum(result.status == "failed" for result in self.results)


def scan_files(inbox: Path, extensions: list[str], recursive: bool) -> list[Path]:
    """Return supported non-hidden files from the inbox."""
    pattern = "**/*" if recursive else "*"
    allowed = {extension.lower() for extension in extensions}
    paths = sorted(inbox.glob(pattern))
    return [
        path
        for path in paths
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in allowed
        and not is_icloud_stub(path)
    ]


def process_file(filepath: Path, config: AppConfig, dry_run: bool) -> RenameResult:
    """Extract metadata, build a filename, and optionally rename the file."""
    try:
        created_at = datetime.fromtimestamp(filepath.stat().st_ctime, tz=UTC)
        preview_text = extract_text_preview(filepath)
        image_base64 = _get_image_payload(filepath, preview_text)
        metadata = extract_metadata(
            filename=filepath.name,
            created_at=created_at,
            preview_text=preview_text,
            image_base64=image_base64,
            config=config,
        )
        new_name = build_filename(
            date_prefix=select_date_prefix(metadata.document_date, created_at.date()),
            sender=metadata.sender,
            subject=metadata.subject,
            extension=filepath.suffix.lower(),
            template=config.filename_template,
            filename_format=metadata.filename_format,
        )
        if dry_run:
            return RenameResult(filepath, filepath.parent / new_name, new_name, "dry_run")
        new_path = rename_file(filepath, filepath.parent, new_name)
        return RenameResult(filepath, new_path, new_name, "renamed")
    except Exception as exc:
        return RenameResult(filepath, None, None, "failed", str(exc))


def run(config: AppConfig, dry_run: bool, compress: bool) -> RunSummary:
    """Run the rename pipeline over configured files."""
    inbox = Path(config.inbox_path)
    results = [_download_stub(stub) for stub in _scan_icloud_stubs(inbox, config.recursive)]
    filepaths = scan_files(inbox, config.file_extensions, config.recursive)
    results.extend(process_file(path, config, dry_run) for path in filepaths)
    if compress and not dry_run:
        _compress_renamed_pdfs(results, config)
    if not dry_run:
        write_undo_log(results, Path("data"))
    return RunSummary(results)


def write_undo_log(results: list[RenameResult], data_dir: Path) -> None:
    """Persist the successful renames from the last run."""
    entries = [
        {"old_path": str(result.old_path), "new_path": str(result.new_path)}
        for result in results
        if result.status == "renamed" and result.new_path is not None
    ]
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / UNDO_FILENAME).write_text(json.dumps(entries, indent=2))


def undo_last_run(data_dir: Path) -> list[tuple[Path, Path]]:
    """Undo the most recent rename run and remove the undo log."""
    undo_path = data_dir / UNDO_FILENAME
    if not undo_path.exists():
        return []
    entries = json.loads(undo_path.read_text())
    reversed_pairs: list[tuple[Path, Path]] = []
    for entry in reversed(entries):
        old_path = Path(entry["old_path"])
        new_path = Path(entry["new_path"])
        if not new_path.exists():
            continue
        old_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(new_path), str(old_path))
        reversed_pairs.append((new_path, old_path))
    undo_path.unlink()
    return reversed_pairs


def _get_image_payload(filepath: Path, preview_text: str) -> str | None:
    """Return a base64 image payload for image files or image-only PDFs."""
    if is_image_file(filepath):
        return encode_image_base64(filepath)
    if filepath.suffix.lower() != ".pdf" or preview_text.strip():
        return None
    temp_image = render_pdf_page(filepath)
    if temp_image is None:
        return None
    try:
        return encode_image_base64(temp_image)
    finally:
        temp_image.unlink(missing_ok=True)


def _scan_icloud_stubs(inbox: Path, recursive: bool) -> list[Path]:
    """Find iCloud stubs in the inbox."""
    pattern = "**/*" if recursive else "*"
    return [path for path in sorted(inbox.glob(pattern)) if path.is_file() and is_icloud_stub(path)]


def _download_stub(stub_path: Path) -> RenameResult:
    """Attempt to materialize an iCloud stub before processing."""
    real_path = resolve_icloud_path(stub_path)
    downloaded = download_icloud_file(stub_path)
    if downloaded is None:
        return RenameResult(stub_path, None, real_path.name, "skipped", "iCloud download failed")
    return RenameResult(stub_path, downloaded, downloaded.name, "skipped")


def _compress_renamed_pdfs(results: list[RenameResult], config: AppConfig) -> None:
    """Compress renamed PDFs in place when enabled."""
    for result in results:
        if result.status != "renamed" or result.new_path is None:
            continue
        if result.new_path.suffix.lower() != ".pdf":
            continue
        temp_path = result.new_path.with_suffix(".compressed.pdf")
        if compress_pdf(
            result.new_path,
            temp_path,
            dpi=config.compress.dpi,
            jpeg_quality=config.compress.jpeg_quality,
        ):
            shutil.move(str(temp_path), str(result.new_path))
        elif temp_path.exists():
            temp_path.unlink()
