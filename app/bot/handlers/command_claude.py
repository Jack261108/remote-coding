from __future__ import annotations

import logging

from aiogram.filters import Command
from aiogram.types import Message

from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


def register_claude_handler(router, *, task_service: TaskService):
    @router.message(Command("claude"))
    async def command_claude(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        try:
            opened, text = await task_service.open_claude_chat_session(user_id)
        except ValueError as exc:
            logger.warning("open claude session validation failed", extra={"user_id": user_id, "error": str(exc)})
            await message.answer(f"开启失败: {exc}")
            return
        except Exception as exc:
            logger.exception("open claude session failed", extra={"user_id": user_id})
            await message.answer(f"开启 Claude 会话失败: {exc}")
            return

        if opened:
            await message.answer(f"{text}\n现在可直接发送文本与 Claude 对话。")
        else:
            await message.answer(f"开启失败: {text}")
