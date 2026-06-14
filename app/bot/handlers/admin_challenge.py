"""Shared helper for initiating admin password challenges."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import Message

if TYPE_CHECKING:
    from app.services.admin_password_service import AdminPasswordService


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
