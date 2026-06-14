from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.user_utils import extract_user_id
from app.services.task_service import TaskService

if TYPE_CHECKING:
    from app.services.admin_password_service import AdminPasswordService


def register_cancel_handler(
    router,
    *,
    task_service: TaskService,
    admin_password_service: AdminPasswordService | None = None,
):
    @router.message(Command("cancel"))
    async def command_cancel(message: Message, command: CommandObject) -> None:
        task_id = (command.args or "").strip()
        user_id = extract_user_id(message)

        # Cancel pending admin password challenge if no task_id given
        if not task_id and admin_password_service is not None and admin_password_service.is_enabled:
            challenge = admin_password_service.cancel(user_id)
            if challenge is not None:
                await message.answer("已取消密码验证。")
                return

        if not task_id:
            await message.answer("用法: /cancel <task_id>")
            return

        canceled = await task_service.cancel(task_id=task_id, user_id=user_id)

        if canceled:
            await message.answer(f"已发送取消请求: {task_id}")
        else:
            await message.answer(f"取消失败，任务不存在或已结束: {task_id}")
