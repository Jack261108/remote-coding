from __future__ import annotations

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.command_utils import command_args
from app.bot.handlers.user_utils import extract_user_id
from app.bot.presenters.session_text import render_structured_session
from app.services.task_service import TaskService


def _render_task(task) -> str:
    duration = f"{task.duration_sec:.2f}s" if task.duration_sec is not None else "-"
    return (
        f"task_id: {task.task_id}\n"
        f"provider: {task.provider}\n"
        f"status: {task.status.value}\n"
        f"exit_code: {task.exit_code}\n"
        f"duration: {duration}\n"
        f"failure: {task.failure_reason or '-'}"
    )


def register_status_handler(router, *, task_service: TaskService):
    @router.message(Command("status"))
    async def command_status(message: Message, command: CommandObject) -> None:
        user_id = extract_user_id(message)
        task_id = command_args(command)

        if task_id:
            task = await task_service.get_status(task_id=task_id, user_id=user_id)
            if task is None:
                await message.answer("未找到该任务。")
                return
            lines = [_render_task(task)]
            if task.provider == "claude_code":
                structured = await task_service.get_structured_session_for_task(task_id=task.task_id, user_id=user_id)
                if structured is not None:
                    lines.append("")
                    lines.append(render_structured_session(structured, include_last_reply=False))
            await message.answer("\n".join(lines))
            return

        tasks = await task_service.list_recent(user_id=user_id, limit=10)
        if not tasks:
            await message.answer("暂无任务记录。")
            return

        lines = ["最近任务:"]
        for task in tasks:
            lines.append(f"- {task.task_id} | {task.provider} | {task.status.value}")
        await message.answer("\n".join(lines))
