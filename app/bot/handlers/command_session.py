from __future__ import annotations

from pathlib import Path

from aiogram.filters import Command
from aiogram.types import Message

from app.services.session_service import SessionService
from app.services.task_service import TaskService


def _render_structured_session(state) -> str:
    last_turn = state.turns[-1] if state.turns else None
    last_reply = (last_turn.text.strip() if last_turn else "") or "-"
    if len(last_reply) > 200:
        last_reply = f"{last_reply[:200].rstrip()}..."
    return (
        "structured_session:\n"
        f"phase: {state.phase.value}\n"
        f"turns: {len(state.turns)}\n"
        f"current_turn_id: {state.current_turn_id or '-'}\n"
        f"last_reply: {last_reply}"
    )


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
            lines = [
                f"session_id: {session.session_id}",
                f"provider: {session.provider}",
                f"workdir: {session.workdir}",
                f"claude_chat_active: {session.claude_chat_active}",
            ]
            structured = await task_service.get_structured_session(user_id)
            if structured is not None:
                lines.append("")
                lines.append(_render_structured_session(structured))
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

        if workdir is not None and not task_service.is_workdir_allowed(workdir):
            await message.answer("workdir 不在白名单中")
            return

        session = await session_service.switch(user_id=user_id, provider=provider, workdir=workdir)
        await message.answer(
            f"session 已更新\n"
            f"session_id: {session.session_id}\n"
            f"provider: {session.provider}\n"
            f"workdir: {session.workdir}\n"
            f"claude_chat_active: {session.claude_chat_active}"
        )
