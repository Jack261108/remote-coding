from __future__ import annotations

import logging
from pathlib import PurePosixPath

from app.adapters.storage.upload_store import UploadStoreAdapter
from app.domain.file_models import FileUploadResult, FileValidationError

logger = logging.getLogger(__name__)


class FileReceiverService:
    """Handles incoming file uploads, validates them, and stores via the adapter."""

    def __init__(
        self,
        *,
        upload_store: UploadStoreAdapter,
        allowed_extensions: set[str],
        max_file_size_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self._upload_store = upload_store
        self._allowed_extensions = {ext.lower() for ext in allowed_extensions}
        self._max_file_size_bytes = max_file_size_bytes

    def validate_extension(self, filename: str) -> bool:
        """Check if file extension is in allowed set (case-insensitive)."""
        ext = PurePosixPath(filename).suffix.lower()
        return ext in self._allowed_extensions

    def validate_size(self, size_bytes: int) -> bool:
        """Check if file size is within limit."""
        return size_bytes <= self._max_file_size_bytes

    async def receive_file(self, *, user_id: int, workdir: str, filename: str, data: bytes) -> FileUploadResult | FileValidationError:
        """Validate and store a single uploaded file."""
        size_bytes = len(data)

        if not self.validate_extension(filename):
            ext = PurePosixPath(filename).suffix or "(no extension)"
            allowed = ", ".join(sorted(self._allowed_extensions))
            return FileValidationError(
                filename=filename,
                reason=f"Extension {ext} is not allowed. Allowed: {allowed}",
            )

        if not self.validate_size(size_bytes):
            limit_mb = self._max_file_size_bytes / (1024 * 1024)
            return FileValidationError(
                filename=filename,
                reason=f"File size ({size_bytes} bytes) exceeds the {limit_mb:.0f} MB limit.",
            )

        path = await self._upload_store.save_file(user_id, workdir, filename, data)
        logger.info(
            "Stored upload: user=%d file=%s size=%d path=%s",
            user_id,
            filename,
            size_bytes,
            path,
        )

        return FileUploadResult(
            filename=path.name,
            size_bytes=size_bytes,
            path=path,
        )
