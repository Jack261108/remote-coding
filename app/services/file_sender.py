from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)


class FileSenderService:
    """Sends files created by Claude back to the user's Telegram chat.

    Classifies files by extension and dispatches via the appropriate
    Telegram method (send_photo for images, send_document for others).
    Never raises — all errors are logged internally.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        enabled: bool,
        extensions: set[str],
        image_extensions: set[str],
        photo_max_bytes: int = 10 * 1024 * 1024,
        document_max_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._bot = bot
        self._enabled = enabled
        self._extensions = extensions
        self._image_extensions = image_extensions
        self._photo_max_bytes = photo_max_bytes
        self._document_max_bytes = document_max_bytes

    def resolve_path(self, file_path_raw: str, cwd: str) -> Path:
        """If file_path_raw is absolute, return as Path. Otherwise join with cwd."""
        p = Path(file_path_raw)
        if p.is_absolute():
            return p
        return Path(cwd) / p

    def classify(self, filename: str) -> str | None:
        """Return 'image', 'document', or None based on extension.

        Handles .tar.gz specially by checking if filename ends with it
        before falling back to Path.suffix.
        """
        if filename.lower().endswith(".tar.gz"):
            ext = ".tar.gz"
        else:
            ext = Path(filename).suffix.lower()

        if not ext or ext not in self._extensions:
            return None

        if ext in self._image_extensions:
            return "image"
        return "document"

    def build_caption(self, file_path: Path, cwd: str) -> str:
        """Return caption like '📎 filename (./relative/path)' relative to cwd."""
        name = file_path.name
        try:
            rel = file_path.relative_to(cwd)
            rel_str = f"./{rel}"
        except ValueError:
            rel_str = str(file_path)
        return f"📎 {name} ({rel_str})"

    async def send_if_eligible(
        self,
        *,
        file_path_raw: str,
        cwd: str,
        chat_id: int,
    ) -> None:
        """Orchestrate: resolve, classify, validate, send. Never raises."""
        try:
            if not self._enabled:
                return

            if not file_path_raw:
                logger.warning("file_sender: empty file_path_raw, skipping")
                return

            file_path = self.resolve_path(file_path_raw, cwd)
            classification = self.classify(file_path.name)

            if classification is None:
                return

            if not file_path.exists():
                logger.warning("file_sender: file does not exist: %s", file_path)
                return

            try:
                size = file_path.stat().st_size
            except OSError as exc:
                logger.warning("file_sender: cannot stat file %s: %s", file_path, exc)
                return

            if classification == "image" and size > self._photo_max_bytes:
                logger.info(
                    "file_sender: image %s exceeds photo limit (%d > %d)",
                    file_path,
                    size,
                    self._photo_max_bytes,
                )
                return

            if classification == "document" and size > self._document_max_bytes:
                logger.info(
                    "file_sender: document %s exceeds document limit (%d > %d)",
                    file_path,
                    size,
                    self._document_max_bytes,
                )
                return

            caption = self.build_caption(file_path, cwd)
            input_file = FSInputFile(file_path)

            if classification == "image":
                await self._bot.send_photo(chat_id, photo=input_file, caption=caption)
            else:
                await self._bot.send_document(chat_id, document=input_file, caption=caption)

            logger.info("file_sender: sent %s as %s to chat %d", file_path, classification, chat_id)

        except Exception:
            logger.exception("file_sender: unexpected error sending %s", file_path_raw)
