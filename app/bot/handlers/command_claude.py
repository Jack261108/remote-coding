from __future__ import annotations

from pathlib import Path

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.user_utils import extract_user_id
from app.services.task_service import TaskService


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
        opened, text = await task_service.open_claude_chat_session(user_id, workdir=workdir)

        if opened:
            await message.answer(f"{text}\n现在可直接发送文本与 Claude 对话。")
        else:
            await message.answer(f"开启失败: {text}")
