from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from app.adapters.cli.factory import CLIAdapterFactory
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.adapters.storage.memory import MemorySessionStore, MemoryTaskStore
from app.bot.middleware.auth import AuthMiddleware
from app.bot.middleware.rate_limit import RateLimitMiddleware
from app.bot.router import create_router
from app.config.settings import Settings
from app.services.session_service import SessionService
from app.services.task_service import TaskService


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        session_kwargs: dict[str, object] = {"timeout": settings.tg_request_timeout_sec}
        if settings.tg_proxy_url:
            session_kwargs["proxy"] = settings.tg_proxy_url

        try:
            session = AiohttpSession(**session_kwargs)
        except RuntimeError as exc:
            if settings.tg_proxy_url and "aiohttp-socks" in str(exc):
                raise RuntimeError(
                    "检测到 TG_PROXY_URL，但缺少 aiohttp-socks。请执行: "
                    "python3 -m pip install aiohttp-socks，或移除 TG_PROXY_URL"
                ) from exc
            raise

        self.bot = Bot(token=settings.tg_bot_token, session=session)
        self.dispatcher = Dispatcher()

        self.task_store = MemoryTaskStore()
        self.session_store = MemorySessionStore()

        self.runner = SubprocessRunner(data_dir=settings.tmux_data_dir)
        self.tmux_runner = TmuxRunner(
            tmux_bin=settings.tmux_bin,
            data_dir=settings.tmux_data_dir,
            claude_cli_bin=settings.claude_cli_bin,
        )
        self.cli_factory = CLIAdapterFactory(
            settings=settings,
            runner=self.runner,
            tmux_runner=self.tmux_runner,
        )

        self.session_service = SessionService(store=self.session_store)
        self.task_service = TaskService(
            settings=settings,
            task_store=self.task_store,
            session_service=self.session_service,
            cli_factory=self.cli_factory,
            semaphore=asyncio.Semaphore(settings.max_concurrent_tasks),
        )

    def wire(self) -> None:
        self.dispatcher.message.middleware(
            AuthMiddleware(
                self.settings.allowed_user_id_set,
                allow_all_users=self.settings.allow_all_users,
            )
        )
        self.dispatcher.message.middleware(
            RateLimitMiddleware(
                limit=self.settings.rate_limit_max_requests,
                window_sec=self.settings.rate_limit_window_sec,
            )
        )

        router = create_router(
            settings=self.settings,
            task_service=self.task_service,
            session_service=self.session_service,
        )
        self.dispatcher.include_router(router)
