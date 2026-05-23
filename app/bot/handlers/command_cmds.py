"""Handler for /cmds — lists available Claude slash commands as inline buttons."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.claude_command_discovery import ClaudeCommand, discover_commands
from app.services.session_service import SessionService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

_SOURCE_ICONS = {
    "builtin": "⚡",
    "user": "👤",
    "project": "📁",
    "skill": "🧠",
}

# Max callback_data is 64 bytes
_CB_PREFIX = "clcmd:"


def _build_callback_data(slash_text: str) -> str:
    """Build callback data, truncating if needed to fit 64 bytes."""
    cb = f"{_CB_PREFIX}{slash_text}"
    if len(cb.encode("utf-8")) > 64:
        max_bytes = 64 - len(_CB_PREFIX.encode("utf-8"))
        cb = _CB_PREFIX + slash_text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return cb


def _parse_callback_data(data: str) -> str | None:
    """Parse callback data to extract the slash command text."""
    if not data.startswith(_CB_PREFIX):
        return None
    return data[len(_CB_PREFIX) :]


def register_cmds_handler(
    router: Router,
    *,
    session_service: SessionService,
    task_service: TaskService,
) -> None:
    @router.message(Command("cmds"))
    async def command_cmds(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        session = await session_service.get(user_id)

        if session is None or not session.claude_chat_active:
            await message.answer("请先发送 /claude 开启会话后再使用 /cmds")
            return

        workdir = session.workdir
        commands = discover_commands(workdir=workdir)

        if not commands:
            await message.answer("未发现可用的 Claude 命令。")
            return

        # Group by source
        groups: dict[str, list[ClaudeCommand]] = {}
        for cmd in commands:
            groups.setdefault(cmd.source, []).append(cmd)

        # Build keyboard: each command as a button
        buttons: list[list[InlineKeyboardButton]] = []

        for source in ["builtin", "user", "skill", "project"]:
            cmds = groups.get(source, [])
            if not cmds:
                continue
            for cmd in cmds:
                icon = _SOURCE_ICONS.get(cmd.source, "")
                label = f"{icon} {cmd.name}"
                if cmd.description and cmd.description != cmd.name.lstrip("/"):
                    label += f" — {cmd.description[:30]}"
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=label,
                            callback_data=_build_callback_data(cmd.slash_text),
                        )
                    ]
                )

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        # Summary text
        counts = ", ".join(f"{_SOURCE_ICONS.get(s, '')} {s}: {len(c)}" for s, c in groups.items() if c)
        await message.answer(f"📋 Claude 命令 ({counts})\n点击发送到当前会话:", reply_markup=keyboard)

    @router.callback_query(F.data.startswith(_CB_PREFIX))
    async def handle_cmd_callback(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        slash_text = _parse_callback_data(callback.data or "")
        if not slash_text:
            await callback.answer("Invalid command", show_alert=True)
            return

        session = await session_service.get(user_id)
        if session is None or not session.claude_chat_active:
            await callback.answer("请先发送 /claude 开启会话", show_alert=True)
            return

        # Send the slash command as a prompt to the Claude session
        from app.bot.handlers.command_run import run_prompt_and_stream
        from app.bot.presenters.chunk_sender import ChunkSender

        await callback.answer(f"发送: {slash_text}")

        # Use the message from the callback to send the stream
        if callback.message:

            def sender_factory() -> ChunkSender:
                return ChunkSender(chunk_size=4000, flush_interval_sec=1.0)

            await run_prompt_and_stream(
                message=callback.message,
                task_service=task_service,
                sender_factory=sender_factory,
                user_id=user_id,
                provider="claude_code",
                prompt=slash_text,
                workdir=session.workdir,
                diff_generator=None,
                result_exporter=None,
            )
