from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
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
from app.bot.adapters.message_sender import AiogramMessageSender
from app.bot.middleware.auth import AuthMiddleware
from app.bot.middleware.rate_limit import RateLimitMiddleware
from app.bot.presenters.permission_message_builder import PermissionMessageBuilder
from app.bot.router import create_router
from app.config.settings import Settings
from app.infra.lock_registry import RefCountedLockRegistry
from app.services.admin_password_service import AdminPasswordService
from app.services.auto_approve_service import AutoApproveService
from app.services.background_task_registry import BackgroundTaskRegistry
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.context_builder import ContextBuilderService
from app.services.diff_generator import DiffGeneratorService
from app.services.external_binding_cleanup_service import ExternalBindingCleanupService
from app.services.external_binding_reaper import ExternalBindingReaper
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.external_session_push_notifier import ExternalSessionPushNotifier
from app.services.file_receiver import FileReceiverService
from app.services.file_sender import FileSenderService
from app.services.jsonl_file_watcher import JSONLFileWatcher
from app.services.periodic_janitor import PeriodicJanitor
from app.services.permission_callback_registry import PermissionCallbackRegistry
from app.services.permission_gateway import PermissionGateway
from app.services.result_exporter import ResultExporterService
from app.services.risk_evaluator import RiskEvaluator
from app.services.session_ownership_resolver import SessionOwnershipResolver
from app.services.session_registry import SessionRegistryService
from app.services.session_scanner import SessionScanner
from app.services.session_service import SessionService
from app.services.session_store import SessionStore
from app.services.session_supervisor import SessionSupervisor
from app.services.status_display import StatusDisplayService
from app.services.task_service import TaskService
from app.services.unbound_permission_handler import UnboundPermissionHandler
from app.services.upload_cleanup import UploadCleanupService
from app.services.upload_queue import UploadQueueManager

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
            session = AiohttpSession(**session_kwargs)  # type: ignore[arg-type]
        except RuntimeError as exc:
            if settings.tg_proxy_url and "aiohttp-socks" in str(exc):
                raise RuntimeError(
                    "检测到 TG_PROXY_URL，但缺少 aiohttp-socks。请执行: python3 -m pip install aiohttp-socks，或移除 TG_PROXY_URL"
                ) from exc
            raise

        self.bot = Bot(token=settings.tg_bot_token, session=session)
        self.dispatcher = Dispatcher()

        self.task_store = MemoryTaskStore(
            max_records=settings.task_store_max_records,
            ttl_hours=settings.task_store_ttl_hours,
        )

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
        self.permission_callback_registry = PermissionCallbackRegistry(
            ttl_sec=settings.claude_hook_pending_permission_ttl_sec,
        )
        self.auto_approve_service = AutoApproveService()
        self.permission_message_builder = PermissionMessageBuilder()
        self.file_session_store = FileSessionStore(settings.tmux_data_dir)
        self.session_context_store = FileSessionContextStore(self.file_session_store)
        self.claude_jsonl_parser = ClaudeJSONLParser(self.claude_paths)
        self.structured_session_store = SessionStore(self.file_session_store)
        self.jsonl_file_watcher = JSONLFileWatcher(
            projects_dir=self.claude_paths.projects_dir,
            debounce_sec=settings.claude_jsonl_sync_debounce_ms / 1000,
            on_change=self._on_jsonl_file_change,
        )
        self.session_supervisor = SessionSupervisor(
            session_store=self.structured_session_store,
            claude_jsonl_parser=self.claude_jsonl_parser,
            on_jsonl_sync=self.sync_claude_session,
            on_dispatch_event=self._dispatch_session_event,
            debounce_sec=settings.claude_jsonl_sync_debounce_ms / 1000,
            jsonl_file_watcher=self.jsonl_file_watcher,
        )
        self.tmux_runner = TmuxRunner(
            tmux_bin=settings.tmux_bin,
            data_dir=settings.tmux_data_dir,
            claude_cli_bin=settings.claude_cli_bin,
            file_store=self.file_session_store,
            session_store=self.structured_session_store,
            session_lock_ttl_sec=settings.session_lock_ttl_sec,
            lock_cleanup_interval_sec=settings.lock_cleanup_interval_sec,
            lock_cleanup_batch_size=settings.lock_cleanup_batch_size,
        )
        self.cli_factory = CLIAdapterFactory(
            settings=settings,
            runner=self.runner,
            tmux_runner=self.tmux_runner,
        )

        self.upload_store = UploadStoreAdapter(base_dir=settings.default_workdir, cleanup_roots=settings.allowed_workdirs)
        self.file_receiver = FileReceiverService(
            upload_store=self.upload_store,
            allowed_extensions=set(settings.allowed_file_extensions),
            max_file_size_bytes=settings.upload_max_file_size_mb * 1024 * 1024,
        )
        self.upload_queue = UploadQueueManager(
            max_files_per_user=settings.upload_queue_max_files_per_user,
            max_bytes_per_user=settings.effective_upload_queue_max_bytes_per_user,
            ttl_sec=settings.upload_queue_ttl_sec,
            cleanup_interval_sec=settings.upload_queue_cleanup_interval_sec,
        )
        self.message_sender = AiogramMessageSender(self.bot)
        self.status_display = StatusDisplayService(bot=self.bot)
        self.file_sender = FileSenderService(
            message_sender=self.message_sender,
            enabled=settings.auto_file_send_enabled,
            extensions=set(settings.auto_file_send_extensions),
            image_extensions={".png", ".jpg", ".jpeg", ".gif", ".webp"},
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
            auto_approve_service=self.auto_approve_service,
        )
        self.session_registry = SessionRegistryService(
            session_service=self.session_service,
            lookup=self.structured_session_store._lookup,
            tmux_runner=self.tmux_runner,
            repository=self.structured_session_store._repository,
            auto_approve_service=self.auto_approve_service,
            health_check_interval_sec=settings.session_health_check_interval_sec,
        )

        # External session services
        self.external_binding_store = ExternalBindingStore(
            data_dir=Path(settings.tmux_data_dir),
        )
        self.external_discovery = ExternalSessionDiscoveryService(
            stale_timeout_sec=settings.external_session_stale_timeout_sec,
            title_resolver=lambda sid, cwd: self.claude_jsonl_parser.extract_session_title(session_id=sid, cwd=cwd),
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
        self.unbound_permission_handler = UnboundPermissionHandler(
            message_sender=self.message_sender,
            hook_socket_server=self.hook_socket_server,
            allowed_user_ids=settings.allowed_user_id_set,
            permission_ttl_sec=settings.claude_hook_pending_permission_ttl_sec,
            title_resolver=lambda sid, cwd: self.claude_jsonl_parser.extract_session_title(session_id=sid, cwd=cwd),
        )
        self.risk_evaluator = RiskEvaluator(
            enabled=settings.risk_eval_enabled,
            dangerous_commands=settings.risk_eval_dangerous_commands,
            dangerous_paths=settings.risk_eval_dangerous_paths,
            protected_paths=settings.risk_eval_protected_paths,
            auto_approve_max_risk=settings.risk_eval_auto_approve_max_risk,
        )
        self.admin_password_service = (
            AdminPasswordService(
                password=settings.admin_password,
            )
            if settings.admin_password
            else None
        )
        self.permission_gateway = PermissionGateway(
            registry=self.permission_callback_registry,
            auto_approve_service=self.auto_approve_service,
            task_service=self.task_service,
            hook_socket_server=self.hook_socket_server,
            unbound_responder=self.unbound_permission_handler,
            settings=settings,
            message_sender=self.message_sender,
            message_builder=self.permission_message_builder,
            risk_evaluator=self.risk_evaluator,
        )
        self.unbound_permission_handler.set_permission_gateway(self.permission_gateway)
        self.push_notifier = ExternalSessionPushNotifier(
            message_sender=self.message_sender,
            binding_store=self.external_binding_store,
            permission_gateway=self.permission_gateway,
            retry_count=settings.push_notification_retry_count,
        )

        # External user question state for PTY injection
        from app.services.external_user_question_state import ExternalUserQuestionState

        self.external_uq_state = ExternalUserQuestionState()

        self.external_binding_reaper = ExternalBindingReaper(
            binding_store=self.external_binding_store,
            auto_approve_service=self.auto_approve_service,
            hook_socket_server=self.hook_socket_server,
            permission_callback_registry=self.permission_callback_registry,
            external_uq_state=self.external_uq_state,
            external_discovery=self.external_discovery,
        )

        self.external_binding_cleanup_service = ExternalBindingCleanupService(
            binding_store=self.external_binding_store,
            hook_socket_server=self.hook_socket_server,
            reaper=self.external_binding_reaper,
            liveness_enabled=settings.external_binding_pid_liveness_enabled,
            ttl=timedelta(hours=settings.external_binding_idle_ttl_hours),
            interval_sec=settings.session_health_check_interval_sec,
        )

        self._jsonl_sync_locks = RefCountedLockRegistry(
            ttl_sec=settings.session_lock_ttl_sec,
            cleanup_interval_sec=settings.lock_cleanup_interval_sec,
            cleanup_batch_size=settings.lock_cleanup_batch_size,
        )
        self._session_event_locks = RefCountedLockRegistry(
            ttl_sec=settings.session_lock_ttl_sec,
            cleanup_interval_sec=settings.lock_cleanup_interval_sec,
            cleanup_batch_size=settings.lock_cleanup_batch_size,
        )
        self._background_tasks = BackgroundTaskRegistry(label="bootstrap")
        self._janitor = PeriodicJanitor()
        self._pending_dead_unbound_cleanup_ids: set[str] = set()
        self._started = False

    def _on_jsonl_file_change(self, session_id: str, cwd: str) -> None:
        """Called from watchdog timer thread -- dispatch to asyncio thread safely."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon_threadsafe(self.session_supervisor.schedule_jsonl_sync, session_id, cwd)

    async def _cleanup_dead_unbound_external_session(self, session_id: str) -> bool:
        """Invalidate pending state for a dead-pruned unbound external session."""

        async def invalidate_external_uq_state() -> int:
            return self.external_uq_state.invalidate_session(session_id)

        cleanup_steps: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
            ("auto approve service", lambda: self.auto_approve_service.clear_session(session_id)),
            ("permission callback registry", lambda: self.permission_callback_registry.invalidate_session(session_id)),
            ("unbound permission handler", lambda: self.unbound_permission_handler.invalidate_session(session_id)),
            ("external user question state", invalidate_external_uq_state),
            ("hook pending permissions", lambda: self.hook_socket_server.cancel_pending_permissions(session_id=session_id)),
        )
        success = True
        for label, cleanup in cleanup_steps:
            try:
                await cleanup()
            except Exception:
                success = False
                logger.exception("dead unbound external session cleanup failed", extra={"session_id": session_id, "step": label})
        if success:
            self._pending_dead_unbound_cleanup_ids.discard(session_id)
        else:
            self._pending_dead_unbound_cleanup_ids.add(session_id)
        return success

    async def _prune_unbound_external_sessions(self) -> None:
        """Prune in-memory unbound external session discovery entries."""
        dead_ids: list[str] = []
        try:
            dead_ids = self.external_discovery.prune_dead()
        except Exception:
            logger.exception("external discovery dead-prune failed")
        self._pending_dead_unbound_cleanup_ids.update(dead_ids)
        for session_id in sorted(self._pending_dead_unbound_cleanup_ids):
            if await self._cleanup_dead_unbound_external_session(session_id):
                self._pending_dead_unbound_cleanup_ids.discard(session_id)
        self.external_discovery.prune_stale()

    async def start(self) -> None:
        if self._started:
            return
        # Register command menu (best-effort)
        try:
            from app.bot.commands import BOT_COMMANDS

            await self.bot.set_my_commands(BOT_COMMANDS)
        except Exception as exc:
            logger.warning("Failed to register bot commands: %s", exc)
        if self.settings.claude_install_hooks:
            self.hook_installer.install()
        self.jsonl_file_watcher.start()
        await self.hook_socket_server.start(self._handle_hook_event, self._handle_permission_failure, self._handle_permission_resolved)
        if self.settings.claude_tmux_mode:
            await self.session_registry.reconcile_terminal_lifecycle()
        await self._restore_session_bindings()

        # Initial cleanup passes (before periodic loop starts)
        await self.external_binding_cleanup_service._cleanup()
        await self.upload_cleanup.run_cleanup()

        # Register periodic jobs
        self._janitor.register(
            "upload_queue_cleanup",
            self.settings.upload_queue_cleanup_interval_sec,
            self.upload_queue.prune_expired,
        )
        self._janitor.register(
            "upload_file_cleanup",
            self.settings.upload_cleanup_interval_min * 60,
            self.upload_cleanup.run_cleanup,
        )
        self._janitor.register(
            "external_binding_cleanup",
            self.settings.session_health_check_interval_sec,
            self.external_binding_cleanup_service._cleanup,
        )
        self._janitor.register(
            "external_discovery_cleanup",
            self.settings.session_health_check_interval_sec,
            self._prune_unbound_external_sessions,
        )
        self._janitor.register(
            "session_health_check",
            self.settings.session_health_check_interval_sec,
            self.session_registry.reconcile_terminal_lifecycle,
        )
        self._janitor.register(
            "periodic_recheck",
            self.settings.claude_periodic_recheck_ms / 1000,
            self._recheck_active_claude_sessions,
        )
        await self._janitor.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            await self.bot.session.close()
            return
        await self._janitor.stop()
        await self.session_supervisor.stop_all()
        self.jsonl_file_watcher.stop()
        await self.hook_socket_server.stop()
        await self._stop_background_tasks()
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
            bucket_ttl_sec=self.settings.effective_rate_limit_bucket_ttl_sec,
            cleanup_interval_sec=self.settings.rate_limit_bucket_cleanup_interval_sec,
            cleanup_batch_size=self.settings.rate_limit_bucket_cleanup_batch_size,
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
            upload_queue=self.upload_queue,
            result_exporter=self.result_exporter,
            diff_generator=self.diff_generator,
            external_discovery=self.external_discovery,
            external_binder=self.external_binder,
            structured_session_store=self.structured_session_store,
            hook_socket_server=self.hook_socket_server,
            unbound_permission_handler=self.unbound_permission_handler,
            external_uq_state=self.external_uq_state,
            permission_gateway=self.permission_gateway,
            session_scanner=SessionScanner(),
            claude_paths=self.claude_paths,
            liveness_enabled=self.settings.external_binding_pid_liveness_enabled,
            external_binding_reaper=self.external_binding_reaper,
            title_resolver=lambda sid, cwd: self.claude_jsonl_parser.extract_session_title(session_id=sid, cwd=cwd),
            dead_unbound_cleanup=self._cleanup_dead_unbound_external_session,
            status_display=self.status_display,
            admin_password_service=self.admin_password_service,
        )
        self.dispatcher.include_router(router)
