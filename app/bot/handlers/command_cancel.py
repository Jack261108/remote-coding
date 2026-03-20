from __future__ import annotations

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.services.task_service import TaskService


def register_cancel_handler(router, *, task_service: TaskService):
    @router.message(Command("cancel"))
    async def command_cancel(message: Message, command: CommandObject) -> None:
        task_id = (command.args or "").strip()
        if not task_id:
            await message.answer("用法: /cancel <task_id>")
            return

        user_id = message.from_user.id if message.from_user else 0
        canceled = await task_service.cancel(task_id=task_id, user_id=user_id)

        if canceled:
            await message.answer(f"已发送取消请求: {task_id}")
        else:
            await message.answer(f"取消失败，任务不存在或已结束: {task_id}")
