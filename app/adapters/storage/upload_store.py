from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

UPLOAD_DIR_NAME = ".tg-uploads"


class UploadStoreAdapter:
    """Manages temporary file storage for user uploads, scoped per user under workdir."""

    def __init__(self, base_dir: str, cleanup_roots: list[str] | None = None) -> None:
        self._base_dir = base_dir
        self._cleanup_roots = cleanup_roots or [base_dir]

    def user_upload_dir(self, user_id: int, workdir: str) -> Path:
        """Returns path: {workdir}/.tg-uploads/{user_id}/"""
        return Path(workdir) / UPLOAD_DIR_NAME / str(user_id)

    async def save_file(self, user_id: int, workdir: str, filename: str, data: bytes) -> Path:
        """Save file with deduplication. Returns final path."""
        target_dir = self.user_upload_dir(user_id, workdir)
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self.deduplicate_filename(target_dir, filename)
        file_path = target_dir / safe_name
        file_path.write_bytes(data)
        return file_path

    def deduplicate_filename(self, target_dir: Path, original_name: str) -> str:
        """Returns unique filename by appending numeric suffix if needed."""
        self._validate_filename(original_name)
        if not (target_dir / original_name).exists():
            return original_name

        stem = Path(original_name).stem
        suffix = Path(original_name).suffix
        counter = 1

        while True:
            candidate = f"{stem}_{counter}{suffix}"
            if not (target_dir / candidate).exists():
                return candidate
            counter += 1

    def _validate_filename(self, filename: str) -> None:
        if not filename:
            raise ValueError("invalid upload filename")
        path = Path(filename)
        if path.name != filename or path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid upload filename")

    def collect_pending_files(self, user_id: int, workdir: str, since: datetime) -> list[Path]:
        """Collect files uploaded since the given timestamp."""
        upload_dir = self.user_upload_dir(user_id, workdir)
        if not upload_dir.exists():
            return []

        since_ts = since.timestamp()
        result: list[Path] = []

        for file_path in upload_dir.iterdir():
            if file_path.is_file() and file_path.stat().st_mtime > since_ts:
                result.append(file_path)

        return sorted(result)

    def clear_user_files(self, user_id: int, workdir: str) -> None:
        """Remove all files for user in workdir upload store."""
        upload_dir = self.user_upload_dir(user_id, workdir)
        if upload_dir.exists():
            shutil.rmtree(upload_dir, ignore_errors=True)

    def cleanup_expired(self, max_age_hours: int = 24) -> int:
        """Delete files older than max_age_hours. Returns count deleted."""
        cutoff = time.time() - (max_age_hours * 3600)
        deleted = 0

        for root in self._cleanup_roots:
            base = Path(root).resolve()
            if not base.is_dir():
                continue

            uploads_dir = base / UPLOAD_DIR_NAME
            if not uploads_dir.is_dir() or uploads_dir.is_symlink():
                continue

            for user_dir in uploads_dir.iterdir():
                if not user_dir.is_dir() or user_dir.is_symlink():
                    continue

                for file_path in user_dir.iterdir():
                    if not file_path.is_file() or file_path.is_symlink():
                        continue
                    try:
                        if file_path.lstat().st_mtime < cutoff:
                            file_path.unlink()
                            deleted += 1
                    except OSError as exc:
                        logger.warning("Failed to delete expired file %s: %s", file_path, exc)

                # Remove empty user directories
                try:
                    if user_dir.exists() and not any(user_dir.iterdir()):
                        user_dir.rmdir()
                except OSError:
                    pass

        return deleted
