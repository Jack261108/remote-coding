from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.services.message_sender import Keyboard

logger = logging.getLogger(__name__)


class AiogramMessageSender:
    """Adapter: implements MessageSender using aiogram Bot."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        parse_mode: str | None = None,
    ) -> None:
        reply_markup = _to_reply_markup(keyboard) if keyboard else None
        await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    async def send_photo(
        self,
        chat_id: int,
        file_path: Path,
        caption: str = "",
    ) -> None:
        await self._bot.send_photo(chat_id, photo=FSInputFile(file_path), caption=caption)

    async def send_document(
        self,
        chat_id: int,
        file_path: Path,
        caption: str = "",
    ) -> None:
        await self._bot.send_document(chat_id, document=FSInputFile(file_path), caption=caption)


def _to_reply_markup(keyboard: Keyboard) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data) for btn in row] for row in keyboard.rows]
    )
