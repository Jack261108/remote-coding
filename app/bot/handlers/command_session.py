from __future__ import annotations

from pathlib import Path

from aiogram.filters import Command
from aiogram.types import Message

from app.services.session_service import SessionService
from app.services.task_service import TaskService


def register_session_handler(router, *, task_service: TaskService, session_service: SessionService):
    @router.message(Command("session"))
    async def command_session(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        args = (message.text or "").split(maxsplit=2)

        if len(args) == 1:
            session = await session_service.get(user_id)
            if session is None:
                await message.answer("当前无 session。")
                return
            terminal_info = session.terminal_id if session.terminal_mode and session.terminal_id else "-"
            await message.answer(
                f"session_id: {session.session_id}\n"
                f"provider: {session.provider}\n"
                f"workdir: {session.workdir}\n"
                f"terminal_mode: {session.terminal_mode}\n"
                f"terminal_id: {terminal_info}\n"
                f"claude_chat_active: {session.claude_chat_active}"
            )
            return

        provider = args[1] if len(args) >= 2 else None
        workdir = str(Path(args[2]).resolve()) if len(args) >= 3 else None

        if provider is not None:
            try:
                provider = task_service.normalize_provider(provider)
            except ValueError as exc:
                await message.answer(str(exc))
                return

        if workdir is not None and not task_service.is_workdir_allowed(workdir):
            await message.answer("workdir 不在白名单中")
            return

        session = await session_service.switch(user_id=user_id, provider=provider, workdir=workdir)
        terminal_info = session.terminal_id if session.terminal_mode and session.terminal_id else "-"
        await message.answer(
            f"session 已更新\n"
            f"session_id: {session.session_id}\n"
            f"provider: {session.provider}\n"
            f"workdir: {session.workdir}\n"
            f"terminal_mode: {session.terminal_mode}\n"
            f"terminal_id: {terminal_info}\n"
            f"claude_chat_active: {session.claude_chat_active}"
        )
