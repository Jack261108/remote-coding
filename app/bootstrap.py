from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from app.adapters.claude.hook_installer import HookInstaller
from app.adapters.claude.hook_socket_server import HookSocketServer
from app.adapters.claude.paths import ClaudePaths
from app.adapters.cli.factory import CLIAdapterFactory
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.adapters.storage.file_session_store import FileSessionStore
from app.adapters.storage.memory import MemoryTaskStore
from app.adapters.storage.upload_store import UploadStoreAdapter
from app.bot.middleware.auth import AuthMiddleware
from app.bot.middleware.rate_limit import RateLimitMiddleware
from app.bot.router import create_router
from app.config.settings import Settings
from app.bootstrap_base import AppContainerBase
from app.bootstrap_mixins import (
    EventDispatchMixin,
    HookHandlingMixin,
    JsonlSyncMixin,
    PeriodicRecheckMixin,
    SessionMatchingMixin,
    SessionRestoreMixin,
    WatcherMixin,
)
from app.services.agent_file_watcher import AgentFileWatcher
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.context_builder import ContextBuilderService
from app.services.diff_generator import DiffGeneratorService
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.external_session_push_notifier import ExternalSessionPushNotifier
from app.services.file_receiver import FileReceiverService
from app.services.interrupt_watcher import InterruptWatcher
from app.services.result_exporter import ResultExporterService
from app.services.session_ownership_resolver import SessionOwnershipResolver
from app.services.session_service import SessionService
from app.services.session_registry import SessionRegistryService
from app.services.session_store import SessionStore
from app.services.task_service import TaskService
from app.services.unbound_permission_handler import UnboundPermissionHandler
from app.services.upload_cleanup import UploadCleanupService

logger = logging.getLogger(__name__)


class AppContainer(
    JsonlSyncMixin,
    HookHandlingMixin,
    SessionMatchingMixin,
    WatcherMixin,
    PeriodicRecheckMixin,
    SessionRestoreMixin,
    EventDispatchMixin,
    AppContainerBase,
):
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
                    "检测到 TG_PROXY_URL，但缺少 aiohttp-socks。请执行: python3 -m pip install aiohttp-socks，或移除 TG_PROXY_URL"
                ) from exc
            raise

        self.bot = Bot(token=settings.tg_bot_token, session=session)
        self.dispatcher = Dispatcher()

        self.task_store = MemoryTaskStore()

        self.runner = SubprocessRunner()
        self.claude_paths = ClaudePaths.resolve(settings.claude_config_dir)
        self.hook_installer = HookInstaller(
            paths=self.claude_paths,
            socket_path=settings.claude_hook_socket_path,
            claude_bin=settings.claude_cli_bin,
        )
        self.hook_socket_server = HookSocketServer(
            settings.claude_hook_socket_path,
            allowed_workdirs=settings.allowed_workdirs,
            max_message_bytes=settings.claude_hook_max_message_bytes,
            pending_permission_ttl_sec=settings.claude_hook_pending_permission_ttl_sec,
            max_pending_permissions=settings.claude_hook_max_pending_permissions,
        )
        self.file_session_store = FileSessionStore(settings.tmux_data_dir)
        self.session_context_store = FileSessionContextStore(self.file_session_store)
        self.claude_jsonl_parser = ClaudeJSONLParser(self.claude_paths)
        self.structured_session_store = SessionStore(self.file_session_store)
        self.interrupt_watcher = InterruptWatcher(
            session_store=self.structured_session_store,
            claude_jsonl_parser=self.claude_jsonl_parser,
        )
        self.agent_file_watcher = AgentFileWatcher(
            session_store=self.structured_session_store,
            claude_jsonl_parser=self.claude_jsonl_parser,
            on_update=self.sync_claude_session,
        )
        self.tmux_runner = TmuxRunner(
            tmux_bin=settings.tmux_bin,
            data_dir=settings.tmux_data_dir,
            claude_cli_bin=settings.claude_cli_bin,
            file_store=self.file_session_store,
            session_store=self.structured_session_store,
        )
        self.cli_factory = CLIAdapterFactory(
            settings=settings,
            runner=self.runner,
            tmux_runner=self.tmux_runner,
        )

        self.upload_store = UploadStoreAdapter(base_dir=settings.default_workdir)
        self.file_receiver = FileReceiverService(
            upload_store=self.upload_store,
            allowed_extensions=set(settings.allowed_file_extensions),
            max_file_size_bytes=settings.upload_max_file_size_mb * 1024 * 1024,
        )
        self.context_builder = ContextBuilderService(upload_store=self.upload_store)
        self.result_exporter = ResultExporterService(settings=settings)
        self.diff_generator = DiffGeneratorService()
        self.upload_cleanup = UploadCleanupService(
            upload_store=self.upload_store,
            interval_minutes=settings.upload_cleanup_interval_min,
            max_age_hours=settings.upload_expiry_hours,
        )

        self.session_service = SessionService(store=self.session_context_store)
        self.task_service = TaskService(
            settings=settings,
            task_store=self.task_store,
            session_service=self.session_service,
            cli_factory=self.cli_factory,
            semaphore=asyncio.Semaphore(settings.max_concurrent_tasks),
            structured_session_store=self.structured_session_store,
            hook_socket_server=self.hook_socket_server,
            context_builder=self.context_builder,
        )
        self.session_registry = SessionRegistryService(
            session_service=self.session_service,
            lookup=self.structured_session_store._lookup,
            tmux_runner=self.tmux_runner,
            repository=self.structured_session_store._repository,
            health_check_interval_sec=settings.session_health_check_interval_sec,
        )

        # External session services
        self.external_binding_store = ExternalBindingStore(
            data_dir=Path(settings.tmux_data_dir),
        )
        self.external_discovery = ExternalSessionDiscoveryService(
            stale_timeout_sec=settings.external_session_stale_timeout_sec,
        )
        self.ownership_resolver = SessionOwnershipResolver(
            session_service=self.session_service,
            binding_store=self.external_binding_store,
        )
        self.external_binder = ExternalSessionBinder(
            discovery=self.external_discovery,
            binding_store=self.external_binding_store,
            projects_dir=Path("~/.claude/projects").expanduser(),
            sync_callback=self.sync_claude_session,
        )
        self.push_notifier = ExternalSessionPushNotifier(
            bot=self.bot,
            binding_store=self.external_binding_store,
            retry_count=settings.push_notification_retry_count,
        )
        self.unbound_permission_handler = UnboundPermissionHandler(
            bot=self.bot,
            hook_socket_server=self.hook_socket_server,
            allowed_user_ids=settings.allowed_user_id_set,
        )

        self._jsonl_sync_tasks: dict[str, asyncio.Task[None]] = {}
        self._jsonl_sync_requests: dict[str, str] = {}
        self._jsonl_sync_locks: dict[str, asyncio.Lock] = {}
        self._session_event_locks: dict[str, asyncio.Lock] = {}
        self._periodic_recheck_task: asyncio.Task[None] | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        if self.settings.claude_install_hooks:
            self.hook_installer.install()
        await self.hook_socket_server.start(self._handle_hook_event, self._handle_permission_failure)
        await self._restore_session_bindings()
        self._start_interrupt_watchers()
        self._start_agent_file_watchers()
        self._periodic_recheck_task = asyncio.create_task(self._periodic_recheck_loop())
        await self.session_registry.start_health_check()
        await self.upload_cleanup.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            await self.bot.session.close()
            return
        await self.upload_cleanup.stop()
        await self.session_registry.stop_health_check()
        await self._stop_periodic_recheck_task()
        await self._stop_jsonl_sync_tasks()
        await self.agent_file_watcher.stop_all()
        await self.interrupt_watcher.stop_all()
        await self.hook_socket_server.stop()
        await self.bot.session.close()
        self._started = False

    def wire(self) -> None:
        auth_middleware = AuthMiddleware(
            self.settings.allowed_user_id_set,
            allow_all_users=self.settings.allow_all_users,
        )
        rate_limit_middleware = RateLimitMiddleware(
            limit=self.settings.rate_limit_max_requests,
            window_sec=self.settings.rate_limit_window_sec,
        )
        self.dispatcher.message.middleware(auth_middleware)
        self.dispatcher.callback_query.middleware(auth_middleware)
        self.dispatcher.message.middleware(rate_limit_middleware)
        self.dispatcher.callback_query.middleware(rate_limit_middleware)

        router = create_router(
            settings=self.settings,
            task_service=self.task_service,
            session_service=self.session_service,
            registry_service=self.session_registry,
            file_receiver=self.file_receiver,
            result_exporter=self.result_exporter,
            diff_generator=self.diff_generator,
            external_discovery=self.external_discovery,
            external_binder=self.external_binder,
            structured_session_store=self.structured_session_store,
            hook_socket_server=self.hook_socket_server,
            unbound_permission_handler=self.unbound_permission_handler,
        )
        self.dispatcher.include_router(router)
