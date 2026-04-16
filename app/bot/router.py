from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.handlers.command_cancel import register_cancel_handler
from app.bot.handlers.command_claude import register_claude_handler
from app.bot.handlers.command_exit import register_exit_handler
from app.bot.handlers.command_permission import register_permission_handlers
from app.bot.handlers.command_run import register_run_handler, run_prompt_and_stream
from app.bot.handlers.command_session import register_session_handler
from app.bot.handlers.command_status import register_status_handler
from app.bot.presenters.chunk_sender import ChunkSender
from app.config.settings import Settings
from app.services.session_service import SessionService
from app.services.task_service import TaskService


def create_router(*, settings: Settings, task_service: TaskService, session_service: SessionService) -> Router:
    router = Router()

    @router.message(Command("start"))
    async def command_start(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        session = await session_service.get(user_id)
        session_text = (
            f"session_id: {session.session_id}\n"
            f"provider: {session.provider}\n"
            f"workdir: {session.workdir}\n"
            f"claude_chat_active: {session.claude_chat_active}"
            if session
            else "session: 尚未创建"
        )
        providers = ", ".join(task_service.available_providers())
        await message.answer(
            "欢迎使用 Telegram CLI Gateway\n"
            "命令:\n"
            "/run <provider> <task text>\n"
            "/claude (开启 Claude 会话模式)\n"
            "/status [task_id]\n"
            "/cancel <task_id>\n"
            "/session [provider] [workdir]\n"
            "/approve\n"
            "/deny [reason]\n"
            "/exit 或 /quit (退出 Claude 会话并关闭持久终端)\n"
            f"可用 provider: {providers}\n"
            f"{session_text}"
        )

    sender_factory = lambda: ChunkSender(
        chunk_size=settings.chunk_size,
        flush_interval_sec=settings.chunk_flush_interval_sec,
    )

    register_run_handler(
        router,
        task_service=task_service,
        sender_factory=sender_factory,
    )
    register_claude_handler(router, task_service=task_service)
    register_cancel_handler(router, task_service=task_service)
    register_status_handler(router, task_service=task_service)
    register_session_handler(router, task_service=task_service, session_service=session_service)
    register_permission_handlers(router, task_service=task_service)
    register_exit_handler(router, task_service=task_service)

    @router.message(F.text & ~F.text.startswith("/"))
    async def command_claude_chat_text(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        user_id = message.from_user.id if message.from_user else 0
        session = await session_service.get(user_id)
        if session is None or not session.claude_chat_active:
            await message.answer("请先发送 /claude")
            return

        await run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=sender_factory,
            user_id=user_id,
            provider="claude_code",
            prompt=text,
            workdir=session.workdir,
        )

    return router
