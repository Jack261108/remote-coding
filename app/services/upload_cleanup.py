from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from app.adapters.storage.upload_store import UploadStoreAdapter

logger = logging.getLogger(__name__)


class UploadCleanupService:
    """Periodic cleanup of expired upload files."""

    def __init__(
        self,
        *,
        upload_store: UploadStoreAdapter,
        interval_minutes: int = 60,
        max_age_hours: int = 24,
    ) -> None:
        self._upload_store = upload_store
        self._interval_minutes = interval_minutes
        self._max_age_hours = max_age_hours
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Run initial cleanup and start periodic task."""
        deleted = await self.run_cleanup()
        logger.info("Initial upload cleanup: deleted %d expired files", deleted)
        self._task = asyncio.create_task(self._periodic_loop())

    async def stop(self) -> None:
        """Cancel periodic cleanup task."""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_cleanup(self) -> int:
        """Execute one cleanup pass. Returns number of files deleted."""
        deleted = self._upload_store.cleanup_expired(self._max_age_hours)
        logger.info("Upload cleanup: deleted %d expired files", deleted)
        return deleted

    async def _periodic_loop(self) -> None:
        """Sleep then cleanup, repeating until cancelled."""
        try:
            while True:
                await asyncio.sleep(self._interval_minutes * 60)
                await self.run_cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Upload cleanup periodic loop failed")
