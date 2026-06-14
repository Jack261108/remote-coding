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
    ) -> int | None:
        return await self._send_or_edit_message(
            chat_id,
            text,
            keyboard=keyboard,
            parse_mode=parse_mode,
        )

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        parse_mode: str | None = None,
    ) -> None:
        await self._send_or_edit_message(
            chat_id,
            text,
            message_id=message_id,
            keyboard=keyboard,
            parse_mode=parse_mode,
        )

    async def send_photo(
        self,
        chat_id: int,
        file_path: Path,
        caption: str = "",
    ) -> None:
        await self._send_media(chat_id, file_path, "photo", caption)

    async def send_document(
        self,
        chat_id: int,
        file_path: Path,
        caption: str = "",
    ) -> None:
        await self._send_media(chat_id, file_path, "document", caption)

    # -- private helpers --------------------------------------------------

    async def _send_media(
        self,
        chat_id: int,
        file_path: Path,
        media_type: str,
        caption: str = "",
    ) -> None:
        """通用媒体发送方法，media_type 为 "photo" 或 "document"。"""
        media = FSInputFile(file_path)
        send = getattr(self._bot, f"send_{media_type}")
        await send(chat_id, **{media_type: media}, caption=caption)

    async def _send_or_edit_message(
        self,
        chat_id: int,
        text: str,
        message_id: int | None = None,
        *,
        keyboard: Keyboard | None = None,
        parse_mode: str | None = None,
    ) -> int | None:
        """通用消息发送/编辑方法。message_id 为 None 时发送新消息，否则编辑已有消息。"""
        reply_markup = _to_reply_markup(keyboard) if keyboard else None
        if message_id is None:
            msg = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return msg.message_id
        await self._bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return None


def _to_reply_markup(keyboard: Keyboard) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data) for btn in row] for row in keyboard.rows]
    )
