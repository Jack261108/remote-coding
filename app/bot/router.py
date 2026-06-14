from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.adapters.claude.paths import ClaudePaths
from app.bot.handlers.command_attach import register_attach_handler
from app.bot.handlers.command_cancel import register_cancel_handler
from app.bot.handlers.command_claude import register_claude_handler
from app.bot.handlers.command_cmds import register_cmds_handler
from app.bot.handlers.command_exit import register_exit_handler
from app.bot.handlers.command_export import register_export_handler
from app.bot.handlers.command_list import register_list_handler
from app.bot.handlers.command_permission import register_permission_handlers
from app.bot.handlers.command_resume import register_resume_handler
from app.bot.handlers.command_run import register_run_handler, run_prompt_and_stream
from app.bot.handlers.command_session import register_session_handler
from app.bot.handlers.command_status import register_status_handler
from app.bot.handlers.command_user_question import maybe_handle_pending_user_question_text, register_user_question_handlers
from app.bot.handlers.external_permission import register_external_permission_handler
from app.bot.handlers.external_session import register_external_session_handler
from app.bot.handlers.file_upload import register_file_upload_handler, schedule_pending_upload_processing
from app.bot.handlers.session_actions import register_session_action_handlers
from app.bot.middleware.callback_validator import CallbackValidatorMiddleware
from app.bot.middleware.error_handling import ErrorHandlingMiddleware
from app.bot.middleware.session_guard import SessionGuardMiddleware
from app.bot.presenters.chunk_sender import ChunkSender
from app.config.settings import Settings
from app.domain.models import SessionContext
from app.services.diff_generator import DiffGeneratorService
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.file_receiver import FileReceiverService
from app.services.result_exporter import ResultExporterService
from app.services.session_registry import SessionRegistryService
from app.services.session_scanner import SessionScanner
from app.services.session_service import SessionService
from app.services.session_store import SessionStore
from app.services.task_service import TaskService
from app.services.upload_queue import UploadQueueManager

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer
    from app.services.external_binding_reaper import ExternalBindingReaper
    from app.services.external_user_question_state import ExternalUserQuestionState
    from app.services.permission_gateway import PermissionGateway
    from app.services.unbound_permission_handler import UnboundPermissionHandler

logger = logging.getLogger(__name__)


def _register_middleware(
    router: Router,
    session_service: SessionService,
) -> tuple[SessionGuardMiddleware, SessionGuardMiddleware]:
    """Register global middleware (error handling, session guards). Returns guard instances."""
    error_handling_middleware = ErrorHandlingMiddleware()
    router.message.middleware(error_handling_middleware)
    router.callback_query.middleware(error_handling_middleware)

    guard_basic = SessionGuardMiddleware(
        session_service,
        require_active=False,
        skip_commands=("/start", "/session", "/claude", "/exit", "/quit"),
        skip_callback_prefixes=("ext_perm:", "ext_uq:", "sess:", "ask:"),
    )
    guard_active = SessionGuardMiddleware(
        session_service,
        require_active=True,
    )
    router.message.middleware(guard_basic)
    router.callback_query.middleware(guard_basic)
    return guard_basic, guard_active


def _register_optional_handlers(
    router: Router,
    *,
    guard_active: SessionGuardMiddleware,
    session_callbacks: CallbackValidatorMiddleware,
    permission_callbacks: CallbackValidatorMiddleware,
    settings: Settings,
    task_service: TaskService,
    session_service: SessionService,
    registry_service: SessionRegistryService | None,
    file_receiver: FileReceiverService | None,
    upload_queue: UploadQueueManager | None,
    result_exporter: ResultExporterService | None,
    external_discovery: ExternalSessionDiscoveryService | None,
    external_binder: ExternalSessionBinder | None,
    structured_session_store: SessionStore | None,
    hook_socket_server: HookSocketServer | None,
    unbound_permission_handler: UnboundPermissionHandler | None,
    external_uq_state: ExternalUserQuestionState | None,
    permission_gateway: PermissionGateway | None,
    session_scanner: SessionScanner | None,
    claude_paths: ClaudePaths | None,
    liveness_enabled: bool,
    external_binding_reaper: ExternalBindingReaper | None,
    title_resolver: Callable[[str, str], str | None] | None,
    dead_unbound_cleanup: Callable[[str], Awaitable[object]] | None,
) -> None:
    """Register optional handlers that depend on service availability."""
    if session_scanner is not None and claude_paths is not None:
        resume_active_router = Router()
        resume_active_router.message.middleware(guard_active)
        resume_active_router.callback_query.middleware(guard_active)
        register_resume_handler(
            resume_active_router,
            session_scanner=session_scanner,
            task_service=task_service,
            claude_paths=claude_paths,
        )
        router.include_router(resume_active_router)

    if registry_service is not None:
        register_list_handler(
            router,
            registry_service=registry_service,
            external_discovery=external_discovery,
            external_binder=external_binder,
            liveness_enabled=liveness_enabled,
            reaper=external_binding_reaper,
            title_resolver=title_resolver,
            dead_unbound_cleanup=dead_unbound_cleanup,
        )
        register_attach_handler(router, registry_service=registry_service)

    if external_discovery is not None and external_binder is not None:
        session_action_router = Router()
        session_action_router.callback_query.middleware(session_callbacks)
        register_session_action_handlers(
            session_action_router,
            discovery=external_discovery,
            binder=external_binder,
            registry_service=registry_service,
        )
        router.include_router(session_action_router)

    if external_discovery is not None and external_binder is not None and structured_session_store is not None:
        register_external_session_handler(
            router,
            discovery=external_discovery,
            binder=external_binder,
            session_store=structured_session_store,
        )

    if hook_socket_server is not None and unbound_permission_handler is not None and permission_gateway is not None:
        ext_perm_router = Router()
        ext_perm_router.callback_query.middleware(permission_callbacks)
        register_external_permission_handler(
            ext_perm_router,
            hook_socket_server=hook_socket_server,
            unbound_permission_handler=unbound_permission_handler,
            external_uq_state=external_uq_state,
            permission_gateway=permission_gateway,
        )
        router.include_router(ext_perm_router)

    if file_receiver is not None and upload_queue is not None:
        upload_guard_router = Router()
        register_file_upload_handler(
            upload_guard_router,
            file_receiver=file_receiver,
            session_service=session_service,
            task_service=task_service,
            upload_queue=upload_queue,
            upload_max_file_size_mb=settings.upload_max_file_size_mb,
            upload_queue_ttl_sec=settings.upload_queue_ttl_sec,
        )
        router.include_router(upload_guard_router)

    if result_exporter is not None:
        register_export_handler(
            router,
            task_service=task_service,
            result_exporter=result_exporter,
        )


def _create_chat_text_router(
    *,
    guard_active: SessionGuardMiddleware,
    task_service: TaskService,
    session_service: SessionService,
    registry_service: SessionRegistryService | None,
    sender_factory: Callable[[], ChunkSender],
    diff_generator: DiffGeneratorService | None,
    result_exporter: ResultExporterService | None,
    queued_upload_scheduler: Callable[[Message, int, str], None] | None,
    permission_gateway: PermissionGateway | None,
    structured_reply_pump_interval_sec: float,
    spinner_initial_delay_sec: float,
    spinner_interval_sec: float,
) -> Router:
    """Create a sub-router for Claude chat text messages (requires active session)."""
    chat_text_router = Router()
    chat_text_router.message.middleware(guard_active)

    @chat_text_router.message(F.text & ~F.text.startswith("/"))
    async def command_claude_chat_text(message: Message, session: SessionContext) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        user_id = message.from_user.id if message.from_user else 0
        if await maybe_handle_pending_user_question_text(message=message, task_service=task_service):
            return
        logger.info(
            "claude chat text received",
            extra={
                "user_id": user_id,
                "text_len": len(text),
                "has_session": True,
                "claude_chat_active": session.claude_chat_active,
                "session_provider": session.provider,
                "session_workdir": session.workdir,
                "session_claude_session_id": session.claude_session_id,
            },
        )

        # Auto-reattach: validate tmux session is still alive
        if registry_service is not None and session.terminal_id:
            reattached = await registry_service.validate_or_reattach(user_id)
            if reattached is not None:
                session = reattached

        stream_task = await run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=sender_factory,
            user_id=user_id,
            provider="claude_code",
            prompt=text,
            workdir=session.workdir,
            diff_generator=diff_generator,
            result_exporter=result_exporter,
            queued_upload_scheduler=queued_upload_scheduler,
            permission_gateway=permission_gateway,
            structured_reply_pump_interval_sec=structured_reply_pump_interval_sec,
            spinner_initial_delay_sec=spinner_initial_delay_sec,
            spinner_interval_sec=spinner_interval_sec,
        )
        logger.info(
            "claude chat stream spawned",
            extra={
                "user_id": user_id,
                "workdir": session.workdir,
                "claude_session_id": session.claude_session_id,
                "task_created": stream_task is not None,
            },
        )

    return chat_text_router


def create_router(
    *,
    settings: Settings,
    task_service: TaskService,
    session_service: SessionService,
    registry_service: SessionRegistryService | None = None,
    file_receiver: FileReceiverService | None = None,
    upload_queue: UploadQueueManager | None = None,
    result_exporter: ResultExporterService | None = None,
    diff_generator: DiffGeneratorService | None = None,
    external_discovery: ExternalSessionDiscoveryService | None = None,
    external_binder: ExternalSessionBinder | None = None,
    structured_session_store: SessionStore | None = None,
    hook_socket_server: HookSocketServer | None = None,
    unbound_permission_handler: UnboundPermissionHandler | None = None,
    external_uq_state: ExternalUserQuestionState | None = None,
    permission_gateway: PermissionGateway | None = None,
    session_scanner: SessionScanner | None = None,
    claude_paths: ClaudePaths | None = None,
    liveness_enabled: bool = False,
    external_binding_reaper: ExternalBindingReaper | None = None,
    title_resolver: Callable[[str, str], str | None] | None = None,
    dead_unbound_cleanup: Callable[[str], Awaitable[object]] | None = None,
) -> Router:
    router = Router()

    # 注册中间件
    _, guard_active = _register_middleware(router, session_service)

    # 回调数据验证中间件
    session_callbacks = CallbackValidatorMiddleware(expected_parts=3, prefix="sess")
    permission_callbacks = CallbackValidatorMiddleware(
        expected_parts=3,
        prefix=("ext_perm", "ext_uq"),
    )
    user_question_callbacks = CallbackValidatorMiddleware(
        expected_parts=(4, 5),
        prefix="ask",
    )

    # /start 命令
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
            "/claude [workdir] (开启 Claude 会话模式)\n"
            "/list (查看活跃会话)\n"
            "/attach <terminal_id> (连接到会话)\n"
            "/detach (断开当前会话)\n"
            "/status [task_id]\n"
            "/cancel <task_id>\n"
            "/session [provider] [workdir]\n"
            "/approve\n"
            "/deny [reason]\n"
            "/exit 或 /quit (退出 Claude 会话并关闭持久终端)\n"
            f"可用 provider: {providers}\n"
            f"{session_text}"
        )

    # Sender 和上传调度工厂
    sender_factory = lambda: ChunkSender(
        chunk_size=settings.chunk_size,
        flush_interval_sec=settings.chunk_flush_interval_sec,
    )

    queued_upload_scheduler = None
    if file_receiver is not None and upload_queue is not None:

        def _queued_upload_scheduler(message: Message, user_id: int, completed_task_id: str) -> None:
            schedule_pending_upload_processing(
                message,
                file_receiver=file_receiver,
                session_service=session_service,
                upload_queue=upload_queue,
                user_id=user_id,
                task_service=task_service,
                completed_task_id=completed_task_id,
            )

        queued_upload_scheduler = _queued_upload_scheduler

    # 核心命令处理器
    register_run_handler(
        router,
        task_service=task_service,
        sender_factory=sender_factory,
        diff_generator=diff_generator,
        result_exporter=result_exporter,
        queued_upload_scheduler=queued_upload_scheduler,
        permission_gateway=permission_gateway,
        structured_reply_pump_interval_sec=settings.structured_reply_pump_interval_sec,
        spinner_initial_delay_sec=settings.spinner_initial_delay_sec,
        spinner_interval_sec=settings.spinner_interval_sec,
    )
    register_claude_handler(router, task_service=task_service)
    register_cancel_handler(router, task_service=task_service)
    register_status_handler(router, task_service=task_service)
    register_session_handler(router, task_service=task_service, session_service=session_service)
    if permission_gateway is not None:
        register_permission_handlers(
            router,
            permission_gateway=permission_gateway,
        )
    uq_router = Router()
    uq_router.callback_query.middleware(user_question_callbacks)
    register_user_question_handlers(uq_router, task_service=task_service)
    router.include_router(uq_router)
    register_exit_handler(router, task_service=task_service)
    # 子路由器：需要活跃会话的命令
    cmds_active_router = Router()
    cmds_active_router.message.middleware(guard_active)
    cmds_active_router.callback_query.middleware(guard_active)
    register_cmds_handler(cmds_active_router, task_service=task_service)
    router.include_router(cmds_active_router)

    # 可选处理器（依赖服务可用性）
    _register_optional_handlers(
        router,
        guard_active=guard_active,
        session_callbacks=session_callbacks,
        permission_callbacks=permission_callbacks,
        settings=settings,
        task_service=task_service,
        session_service=session_service,
        registry_service=registry_service,
        file_receiver=file_receiver,
        upload_queue=upload_queue,
        result_exporter=result_exporter,
        external_discovery=external_discovery,
        external_binder=external_binder,
        structured_session_store=structured_session_store,
        hook_socket_server=hook_socket_server,
        unbound_permission_handler=unbound_permission_handler,
        external_uq_state=external_uq_state,
        permission_gateway=permission_gateway,
        session_scanner=session_scanner,
        claude_paths=claude_paths,
        liveness_enabled=liveness_enabled,
        external_binding_reaper=external_binding_reaper,
        title_resolver=title_resolver,
        dead_unbound_cleanup=dead_unbound_cleanup,
    )

    # Claude 聊天文本子路由器
    chat_text_router = _create_chat_text_router(
        guard_active=guard_active,
        task_service=task_service,
        session_service=session_service,
        registry_service=registry_service,
        sender_factory=sender_factory,
        diff_generator=diff_generator,
        result_exporter=result_exporter,
        queued_upload_scheduler=queued_upload_scheduler,
        permission_gateway=permission_gateway,
        structured_reply_pump_interval_sec=settings.structured_reply_pump_interval_sec,
        spinner_initial_delay_sec=settings.spinner_initial_delay_sec,
        spinner_interval_sec=settings.spinner_interval_sec,
    )
    router.include_router(chat_text_router)

    return router
