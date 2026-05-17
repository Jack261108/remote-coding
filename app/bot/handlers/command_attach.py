from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.services.session_registry import SessionRegistryService

logger = logging.getLogger(__name__)


def register_attach_handler(router: Router, *, registry_service: SessionRegistryService) -> None:
    @router.message(Command("attach"))
    async def command_attach(message: Message, command: CommandObject) -> None:
        user_id = message.from_user.id if message.from_user else 0
        terminal_id = (command.args or "").strip()

        if not terminal_id:
            # Show the session list instead
            sessions = await registry_service.list_active_sessions()
            if not sessions:
                await message.answer("当前无活跃会话。")
                return
            lines = ["请指定要连接的会话 ID:\n"]
            for s in sessions:
                lines.append(f"  `{s.terminal_id}` ({s.workdir})")
            lines.append("\n用法: /attach <terminal_id>")
            await message.answer("\n".join(lines))
            return

        ok, text = await registry_service.attach_user(user_id=user_id, terminal_id=terminal_id)
        await message.answer(text)

    @router.message(Command("detach"))
    async def command_detach(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        ok, text = await registry_service.detach_user(user_id=user_id)
        await message.answer(text)
