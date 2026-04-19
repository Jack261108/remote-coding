from __future__ import annotations

import logging
from pathlib import Path

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


def resolve_claude_workdir_arg(arg_text: str | None) -> str | None:
    if not arg_text or not arg_text.strip():
        return None
    return str(Path(arg_text.strip()).resolve())


def register_claude_handler(router, *, task_service: TaskService):
    @router.message(Command("claude"))
    async def command_claude(message: Message, command: CommandObject) -> None:
        user_id = message.from_user.id if message.from_user else 0
        workdir = resolve_claude_workdir_arg(command.args)
        if workdir is not None:
            if not task_service.is_workdir_allowed(workdir):
                await message.answer("workdir 不在白名单中")
                return
        try:
            opened, text = await task_service.open_claude_chat_session(user_id, workdir=workdir)
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
