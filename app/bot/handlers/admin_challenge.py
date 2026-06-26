"""Shared helper for initiating admin password challenges."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import Message

from app.bot.handlers.user_utils import extract_user_id
from app.services.admin_password_service import VerifyResult

if TYPE_CHECKING:
    from app.services.admin_password_service import AdminPasswordService
    from app.services.session_service import SessionService
    from app.services.task_service import TaskService


async def maybe_start_admin_challenge(
    message: Message,
    user_id: int,
    workdir: str,
    action: str,
    admin_password_service: AdminPasswordService | None,
    **challenge_kwargs,
) -> bool:
    """Try to start an admin password challenge for an unlisted workdir.

    Returns ``True`` if a challenge was started **or** an error message was
    sent (caller should ``return`` immediately).

    Returns ``False`` if the admin-password service is not available or not
    enabled (caller should fall back to its own rejection logic, e.g.
    ``"workdir 不在白名单中"``).
    """
    if admin_password_service is None or not admin_password_service.is_enabled:
        return False

    if not Path(workdir).is_dir():
        await message.answer(f"workdir 不存在或不是目录: {workdir}")
        return True

    started = admin_password_service.start_challenge(user_id, workdir, action, **challenge_kwargs)
    if not started:
        await message.answer("已有待处理的密码验证，请先输入密码或 /cancel 取消。")
        return True

    await message.answer(f"目录 {workdir} 不在白名单中，请输入管理员密码以继续（或 /cancel 取消）")
    return True


async def maybe_handle_admin_password_text(
    message: Message,
    *,
    task_service: TaskService,
    session_service: SessionService,
    admin_password_service: AdminPasswordService | None,
) -> bool:
    """Handle plain-text replies to a pending admin password challenge."""
    if admin_password_service is None or not admin_password_service.is_enabled:
        return False

    user_id = extract_user_id(message)
    if not user_id or not admin_password_service.has_pending(user_id):
        return False

    password = (message.text or "").strip()
    outcome = admin_password_service.verify(user_id, password)
    if outcome.result is VerifyResult.NO_CHALLENGE:
        await message.answer("密码验证已过期，请重新执行命令。")
        return True
    if outcome.result is VerifyResult.MAX_ATTEMPTS_EXCEEDED:
        await message.answer("密码错误次数过多，已取消验证。")
        return True
    if outcome.result is VerifyResult.WRONG_PASSWORD:
        await message.answer("密码错误，请重试或发送 /cancel 取消。")
        return True

    challenge = outcome.challenge
    if challenge is None or challenge.action != "session":
        await message.answer("不支持的密码验证请求，请重新执行命令。")
        return True

    workdir = challenge.workdir
    if not Path(workdir).is_dir():
        await message.answer(f"workdir 不存在或不是目录: {workdir}")
        return True

    session, orphaned = await session_service.switch(user_id=user_id, provider=challenge.provider, workdir=workdir)
    if orphaned is not None:
        await task_service.cleanup_orphaned_terminal(
            orphaned.terminal_id,
            claude_session_id=orphaned.claude_session_id,
            user_id=orphaned.user_id,
        )
    await message.answer(
        f"session 已更新\n"
        f"session_id: {session.session_id}\n"
        f"provider: {session.provider}\n"
        f"workdir: {session.workdir}\n"
        f"claude_chat_active: {session.claude_chat_active}"
    )
    return True
