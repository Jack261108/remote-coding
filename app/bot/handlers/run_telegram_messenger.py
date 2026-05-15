from __future__ import annotations

import logging

from aiogram.enums import ParseMode
from aiogram.types import Message

from app.bot.presenters.telegram_formatting import render_markdownish_to_telegram_html, split_telegram_html

logger = logging.getLogger(__name__)


class RunTelegramMessenger:
    def __init__(self, *, root_message: Message, task_id: str, user_id: int, provider: str) -> None:
        self._root_message = root_message
        self._task_id = task_id
        self._user_id = user_id
        self._provider = provider

    async def send_message_safely(self, text: str, *, reply_markup=None) -> Message | None:
        if not text:
            return None
        try:
            rendered = render_markdownish_to_telegram_html(text)
            chunks = split_telegram_html(rendered, 4096)
            sent_message = None
            for index, chunk in enumerate(chunks):
                sent_message = await self._root_message.answer(
                    chunk,
                    reply_markup=reply_markup if index == len(chunks) - 1 else None,
                    parse_mode=ParseMode.HTML,
                )
            return sent_message
        except Exception:
            logger.exception(
                "telegram answer failed",
                extra={"task_id": self._task_id, "user_id": self._user_id, "provider": self._provider},
            )
            return None

    async def answer_safely(self, text: str, *, reply_markup=None) -> bool:
        return await self.send_message_safely(text, reply_markup=reply_markup) is not None

    async def edit_message_safely(self, target_message: Message | None, text: str) -> bool:
        if target_message is None or not text:
            return False
        try:
            rendered = render_markdownish_to_telegram_html(text)
            chunks = split_telegram_html(rendered, 4096)
            if len(chunks) != 1:
                return False
            await target_message.edit_text(chunks[0], parse_mode=ParseMode.HTML)
            return True
        except Exception:
            logger.exception(
                "telegram edit failed",
                extra={"task_id": self._task_id, "user_id": self._user_id, "provider": self._provider},
            )
            return False
