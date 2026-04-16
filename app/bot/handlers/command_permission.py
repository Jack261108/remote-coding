from __future__ import annotations

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.services.task_service import TaskService


def register_permission_handlers(router, *, task_service: TaskService):
    @router.message(Command("approve"))
    async def command_approve(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        ok, text = await task_service.respond_to_pending_permission(user_id=user_id, decision="allow")
        if ok:
            await message.answer(text)
        else:
            await message.answer(f"批准失败: {text}")

    @router.message(Command("deny"))
    async def command_deny(message: Message, command: CommandObject) -> None:
        user_id = message.from_user.id if message.from_user else 0
        reason = (command.args or "").strip() or None
        ok, text = await task_service.respond_to_pending_permission(user_id=user_id, decision="deny", reason=reason)
        if ok:
            await message.answer(text)
        else:
            await message.answer(f"拒绝失败: {text}")
