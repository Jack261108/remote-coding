from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.message_sender import BaseMessageSender, Keyboard

logger = logging.getLogger(__name__)


class AiogramMessageSender(BaseMessageSender):
    """Adapter: implements MessageSender using aiogram Bot."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    # -- private helpers --------------------------------------------------

    async def _send_media(
        self,
        chat_id: int,
        media: Any,
        media_type: str,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> Message:
        """通用媒体发送方法，media_type 为 "photo" 或 "document"。"""
        input_file = FSInputFile(media) if isinstance(media, Path) else media
        send = getattr(self._bot, f"send_{media_type}")
        return cast(
            Message,
            await send(chat_id, **{media_type: input_file}, caption=caption, reply_to_message_id=reply_to_message_id),
        )

    async def _send_or_edit_message(
        self,
        chat_id: int,
        text: str,
        message_id: int | None = None,
        parse_mode: str | None = "HTML",
        *,
        keyboard: Keyboard | None = None,
    ) -> Message:
        """通用消息发送/编辑方法。message_id 为 None 时发送新消息，否则编辑已有消息。"""
        reply_markup = _to_reply_markup(keyboard) if keyboard else None
        if message_id is None:
            return await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        message = await self._bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return cast(Message, message)


def _to_reply_markup(keyboard: Keyboard) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data) for btn in row] for row in keyboard.rows]
    )
