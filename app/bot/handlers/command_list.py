from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services.session_registry import SessionRegistryService

logger = logging.getLogger(__name__)

_PHASE_ICONS = {
    "idle": "\u23f8",
    "processing": "\u2699\ufe0f",
    "waiting_for_input": "\U0001f4ac",
    "waiting_for_approval": "\U0001f510",
    "compacting": "\U0001f504",
    "ended": "\u23f9\ufe0f",
}


def register_list_handler(router: Router, *, registry_service: SessionRegistryService) -> None:
    @router.message(Command("list"))
    async def command_list(message: Message) -> None:
        sessions = await registry_service.list_active_sessions()
        if not sessions:
            await message.answer("当前无活跃会话。")
            return

        lines = ["活跃会话:"]
        for s in sessions:
            icon = _PHASE_ICONS.get(s.phase, "\u2753")
            owner_tag = f" (owner:{s.owner_user_id})" if s.owner_user_id else ""
            attached = f" +{len(s.attached_user_ids)}人" if s.attached_user_ids else ""
            alive_tag = "" if s.is_alive else " [已断开]"
            lines.append(f"\n{icon} `{s.terminal_id}`{owner_tag}{attached}{alive_tag}\n   workdir: {s.workdir}\n   phase: {s.phase}")

        lines.append("\n使用 /attach <terminal_id> 连接到会话")
        await message.answer("\n".join(lines))
