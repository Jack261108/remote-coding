from __future__ import annotations

from aiogram.filters import Command
from aiogram.types import Message

from app.services.task_service import TaskService


def register_exit_handler(router, *, task_service: TaskService):
    @router.message(Command("exit"))
    @router.message(Command("quit"))
    async def command_exit(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        closed, text = await task_service.close_terminal(user_id)
        if closed:
            await message.answer("已退出 Claude 会话，再次使用请发送 /claude")
        else:
            await message.answer(text)
