from __future__ import annotations

import logging
from pathlib import Path

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.user_utils import extract_user_id
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


def resolve_claude_workdir_arg(arg_text: str | None) -> str | None:
    if not arg_text or not arg_text.strip():
        return None
    return str(Path(arg_text.strip()).resolve())


def register_claude_handler(router, *, task_service: TaskService):
    @router.message(Command("claude"))
    async def command_claude(message: Message, command: CommandObject) -> None:
        user_id = extract_user_id(message)
        workdir = resolve_claude_workdir_arg(command.args)
        if workdir is not None:
            if not task_service.is_workdir_allowed(workdir):
                await message.answer("workdir 不在白名单中")
                return
        try:
            opened, text = await task_service.open_claude_chat_session(user_id, workdir=workdir)
        except ValueError as exc:
            await message.answer(f"参数错误: {exc}")
            return
        except Exception:
            logger.exception("failed to open claude chat session", extra={"user_id": user_id, "workdir": workdir})
            await message.answer("开启失败，请稍后重试")
            return

        if opened:
            await message.answer(f"{text}\n现在可直接发送文本与 Claude 对话。")
        else:
            await message.answer(f"开启失败: {text}")
