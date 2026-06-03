"""Handler for /resume command — list and resume past Claude sessions."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.adapters.claude.paths import ClaudePaths
from app.bot.handlers.user_utils import extract_user_id
from app.services.session_scanner import SessionInfo, SessionScanner
from app.services.session_service import SessionService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "resume:"


def _format_button_label(info: SessionInfo) -> str:
    """Format button label as 'MM-DD HH:MM | <summary[:30]>...'."""
    date_part = info.modified_at.strftime("%m-%d %H:%M")
    summary = info.summary.strip() if info.summary else ""
    if not summary:
        summary_part = "(no prompt)"
    elif len(summary) > 30:
        summary_part = summary[:30] + "..."
    else:
        summary_part = summary
    return f"{date_part} | {summary_part}"


def register_resume_handler(
    router: Router,
    *,
    session_scanner: SessionScanner,
    task_service: TaskService,
    session_service: SessionService,
    claude_paths: ClaudePaths,
) -> None:
    @router.message(Command("resume"))
    async def command_resume(message: Message) -> None:
        user_id = extract_user_id(message)

        session = await session_service.get(user_id)
        if session is None or not session.workdir:
            await message.answer("请先使用 /claude 开启会话")
            return

        sessions = session_scanner.scan(session.workdir, claude_paths)
        if not sessions:
            await message.answer("当前工作目录无可恢复的会话")
            return

        buttons = [[InlineKeyboardButton(text=_format_button_label(s), callback_data=f"{CALLBACK_PREFIX}{s.session_id}")] for s in sessions]
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer("选择要恢复的会话:", reply_markup=keyboard)

    @router.callback_query(lambda cb: cb.data and cb.data.startswith(CALLBACK_PREFIX))
    async def callback_resume(callback: CallbackQuery) -> None:
        user_id = extract_user_id(callback)
        data = callback.data or ""
        session_id = data[len(CALLBACK_PREFIX) :]

        if not session_id:
            await callback.answer("无效的会话 ID", show_alert=True)
            return

        session = await session_service.get(user_id)
        if session is None or not session.workdir:
            await callback.answer("请先使用 /claude 开启会话", show_alert=True)
            return

        # Verify session file still exists
        encoded_workdir = SessionScanner.encode_workdir(session.workdir)
        session_file = claude_paths.projects_dir / encoded_workdir / f"{session_id}.jsonl"
        if not session_file.is_file():
            await callback.answer("该会话文件已不存在", show_alert=True)
            return

        try:
            # Close existing terminal and start resumed session
            opened, text = await task_service.open_claude_resume_session(user_id, session_id=session_id, workdir=session.workdir)
        except Exception as exc:
            logger.exception("resume session failed", extra={"user_id": user_id, "session_id": session_id})
            await callback.answer(f"恢复失败: {exc}", show_alert=True)
            return

        if opened:
            await callback.answer("会话已恢复")
            if callback.message:
                await callback.message.answer(f"已恢复会话: {session_id[:8]}...\n{text}")
        else:
            await callback.answer(f"恢复失败: {text}", show_alert=True)
