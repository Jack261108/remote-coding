"""Result exporter service for Markdown and ZIP export of task outputs."""

from __future__ import annotations

import fnmatch
import logging
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from app.config.settings import Settings
from app.domain.file_models import ExportResult
from app.domain.models import TaskRecord

logger = logging.getLogger(__name__)


class ResultExporterService:
    """Generates Markdown and ZIP exports of task results."""

    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings

    def format_markdown_header(
        self,
        *,
        task_id: str,
        provider: str,
        duration_sec: float | None,
        timestamp: datetime,
    ) -> str:
        """Format the Markdown header with task metadata."""
        duration_str = f"{duration_sec:.1f}s" if duration_sec is not None else "N/A"
        timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            f"# Task Result: {task_id}\n"
            f"\n"
            f"- **Provider:** {provider}\n"
            f"- **Duration:** {duration_str}\n"
            f"- **Timestamp:** {timestamp_str}\n"
            f"\n"
            f"---\n"
            f"\n"
        )

    async def export_markdown(self, record: TaskRecord) -> ExportResult:
        """Generate a Markdown file from a task record."""
        header = self.format_markdown_header(
            task_id=record.task_id,
            provider=record.provider,
            duration_sec=record.duration_sec,
            timestamp=record.created_at,
        )

        # Build content: header + task output (prompt as fallback if no output stored)
        content = header + (record.prompt if not hasattr(record, "_output") else record.prompt)
        # The actual output text would come from session store; for now use prompt as placeholder
        # In practice, the caller provides the output text or it's stored on the record

        tmp_dir = Path(tempfile.mkdtemp(prefix="export_"))
        filename = f"task_{record.task_id}.md"
        file_path = tmp_dir / filename
        file_path.write_text(content, encoding="utf-8")

        return ExportResult(
            file_path=file_path,
            filename=filename,
            mime_type="text/markdown",
        )

    async def export_zip(
        self,
        record: TaskRecord,
        *,
        workdir: str,
        started_at: datetime,
        ended_at: datetime,
    ) -> ExportResult:
        """Generate a ZIP archive with output + modified files."""
        # First, generate the markdown export
        md_result = await self.export_markdown(record)

        # Collect gitignore patterns
        gitignore_patterns = self._load_gitignore_patterns(workdir)

        # Collect modified files in the time range
        modified_files = self.collect_modified_files(
            workdir=workdir,
            started_at=started_at,
            ended_at=ended_at,
            gitignore_patterns=gitignore_patterns,
        )

        # Create ZIP archive
        tmp_dir = Path(tempfile.mkdtemp(prefix="export_zip_"))
        zip_filename = f"task_{record.task_id}.zip"
        zip_path = tmp_dir / zip_filename

        max_size_bytes = self._settings.zip_max_size_mb * 1024 * 1024
        total_size = md_result.file_path.stat().st_size

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Add markdown output
            zf.write(md_result.file_path, arcname=md_result.filename)

            # Add modified files
            workdir_path = Path(workdir)
            for file_path in modified_files:
                try:
                    file_size = file_path.stat().st_size
                    total_size += file_size
                    if total_size > max_size_bytes:
                        logger.warning(
                            "ZIP size limit exceeded (%d MB), stopping file collection",
                            self._settings.zip_max_size_mb,
                        )
                        raise _ZipSizeLimitExceeded()
                    # Use relative path from workdir as archive name
                    arcname = str(file_path.relative_to(workdir_path))
                    zf.write(file_path, arcname=arcname)
                except _ZipSizeLimitExceeded:
                    raise
                except Exception:
                    logger.warning("Failed to add file to ZIP: %s", file_path, exc_info=True)

        # Check final ZIP size
        final_size = zip_path.stat().st_size
        if final_size > max_size_bytes:
            zip_path.unlink(missing_ok=True)
            raise ZipSizeLimitError(f"ZIP archive exceeds {self._settings.zip_max_size_mb} MB limit. " "Consider using a narrower scope.")

        return ExportResult(
            file_path=zip_path,
            filename=zip_filename,
            mime_type="application/zip",
        )

    def should_auto_export(self, output_chars: int) -> bool:
        """Returns True if output exceeds auto-export threshold (4096)."""
        return output_chars > self._settings.auto_export_threshold_chars

    def collect_modified_files(
        self,
        *,
        workdir: str,
        started_at: datetime,
        ended_at: datetime,
        gitignore_patterns: list[str],
    ) -> list[Path]:
        """Collect files modified within the task time range, excluding gitignored paths."""
        workdir_path = Path(workdir)
        if not workdir_path.is_dir():
            return []

        start_ts = started_at.timestamp()
        end_ts = ended_at.timestamp()

        modified: list[Path] = []

        for file_path in workdir_path.rglob("*"):
            if not file_path.is_file():
                continue

            # Check mtime is within task time range (strictly within)
            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                continue

            if mtime <= start_ts or mtime >= end_ts:
                continue

            # Check gitignore patterns
            rel_path = str(file_path.relative_to(workdir_path))
            if self._matches_gitignore(rel_path, gitignore_patterns):
                continue

            modified.append(file_path)

        return sorted(modified)

    def _matches_gitignore(self, rel_path: str, patterns: list[str]) -> bool:
        """Check if a relative path matches any gitignore pattern."""
        for pattern in patterns:
            # Handle directory patterns (ending with /)
            clean_pattern = pattern.rstrip("/")

            # Match against full relative path
            if fnmatch.fnmatch(rel_path, clean_pattern):
                return True
            # Match against filename only
            if fnmatch.fnmatch(Path(rel_path).name, clean_pattern):
                return True
            # Match against any path component
            parts = Path(rel_path).parts
            for part in parts:
                if fnmatch.fnmatch(part, clean_pattern):
                    return True

        return False

    def _load_gitignore_patterns(self, workdir: str) -> list[str]:
        """Load gitignore patterns from .gitignore file in workdir."""
        gitignore_path = Path(workdir) / ".gitignore"
        if not gitignore_path.is_file():
            return []

        patterns: list[str] = []
        try:
            for line in gitignore_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                patterns.append(line)
        except OSError:
            logger.warning("Failed to read .gitignore at %s", gitignore_path)

        return patterns


class ZipSizeLimitError(Exception):
    """Raised when ZIP archive exceeds the configured size limit."""


class _ZipSizeLimitExceeded(Exception):
    """Internal signal for ZIP size limit during construction."""
