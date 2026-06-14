from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.filters import Command
from aiogram.types import Message

from app.bot.handlers.admin_challenge import maybe_start_admin_challenge
from app.bot.handlers.user_utils import extract_user_id
from app.bot.presenters.session_text import render_structured_session
from app.services.session_service import SessionService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.admin_password_service import AdminPasswordService


def register_session_handler(
    router,
    *,
    task_service: TaskService,
    session_service: SessionService,
    admin_password_service: AdminPasswordService | None = None,
):
    @router.message(Command("session"))
    async def command_session(message: Message) -> None:
        user_id = extract_user_id(message)
        args = (message.text or "").split(maxsplit=2)

        if len(args) == 1:
            session = await session_service.get(user_id)
            if session is None:
                await message.answer("当前无 session。")
                return
            lines = [
                f"session_id: {session.session_id}",
                f"provider: {session.provider}",
                f"workdir: {session.workdir}",
                f"claude_chat_active: {session.claude_chat_active}",
            ]
            structured = await task_service.get_structured_session(user_id)
            if structured is not None:
                lines.append("")
                lines.append(render_structured_session(structured))
            await message.answer("\n".join(lines))
            return

        provider = args[1] if len(args) >= 2 else None
        workdir = str(Path(args[2]).resolve()) if len(args) >= 3 else None

        if provider is not None:
            try:
                provider = task_service.normalize_provider(provider)
            except ValueError as exc:
                await message.answer(str(exc))
                return

        if workdir is not None:
            if not task_service.is_workdir_allowed(workdir):
                if await maybe_start_admin_challenge(message, user_id, workdir, "session", admin_password_service, provider=provider):
                    return
                await message.answer("workdir 不在白名单中")
                return
            if not Path(workdir).is_dir():
                await message.answer(f"workdir 不存在或不是目录: {workdir}")
                return

        session, orphaned = await session_service.switch(user_id=user_id, provider=provider, workdir=workdir)
        # Clean up orphaned terminal resources if detected
        if orphaned is not None:
            logger.info(
                "cleaning up orphaned terminal",
                extra={
                    "terminal_id": orphaned.terminal_id,
                    "claude_session_id": orphaned.claude_session_id,
                    "user_id": orphaned.user_id,
                },
            )
        await message.answer(
            f"session 已更新\n"
            f"session_id: {session.session_id}\n"
            f"provider: {session.provider}\n"
            f"workdir: {session.workdir}\n"
            f"claude_chat_active: {session.claude_chat_active}"
        )
