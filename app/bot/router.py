from __future__ import annotations

import logging
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
from app.bot.handlers.command_resume import register_resume_handler
from app.bot.handlers.external_session import register_external_session_handler
from app.bot.handlers.session_actions import register_session_action_handlers
from app.bot.handlers.external_permission import register_external_permission_handler
from app.bot.handlers.command_permission import register_permission_handlers
from app.bot.handlers.command_user_question import maybe_handle_pending_user_question_text, register_user_question_handlers
from app.bot.handlers.command_run import register_run_handler, run_prompt_and_stream
from app.bot.handlers.command_session import register_session_handler
from app.bot.handlers.command_status import register_status_handler
from app.bot.handlers.file_upload import register_file_upload_handler, schedule_pending_upload_processing
from app.bot.presenters.chunk_sender import ChunkSender
from app.config.settings import Settings
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
    from app.services.auto_approve_service import AutoApproveService
    from app.services.external_user_question_state import ExternalUserQuestionState
    from app.services.permission_callback_registry import PermissionCallbackRegistry
    from app.services.permission_gateway import PermissionGateway
    from app.services.unbound_permission_handler import UnboundPermissionHandler

logger = logging.getLogger(__name__)


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
    auto_approve_service: AutoApproveService | None = None,
    permission_callback_registry: PermissionCallbackRegistry | None = None,
    permission_gateway: PermissionGateway | None = None,
    session_scanner: SessionScanner | None = None,
    claude_paths: ClaudePaths | None = None,
) -> Router:
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

    register_run_handler(
        router,
        task_service=task_service,
        sender_factory=sender_factory,
        diff_generator=diff_generator,
        result_exporter=result_exporter,
        queued_upload_scheduler=queued_upload_scheduler,
        permission_callback_registry=permission_callback_registry,
        permission_gateway=permission_gateway,
    )
    register_claude_handler(router, task_service=task_service)
    register_cancel_handler(router, task_service=task_service)
    register_status_handler(router, task_service=task_service)
    register_session_handler(router, task_service=task_service, session_service=session_service)
    if permission_callback_registry is not None or permission_gateway is not None:
        register_permission_handlers(
            router,
            task_service=task_service,
            auto_approve_service=auto_approve_service,
            hook_socket_server=hook_socket_server,
            structured_session_store=structured_session_store,
            permission_callback_registry=permission_callback_registry,
            permission_gateway=permission_gateway,
        )
    register_user_question_handlers(router, task_service=task_service)
    register_exit_handler(router, task_service=task_service)
    register_cmds_handler(router, session_service=session_service, task_service=task_service)

    if session_scanner is not None and claude_paths is not None:
        register_resume_handler(
            router,
            session_scanner=session_scanner,
            task_service=task_service,
            session_service=session_service,
            claude_paths=claude_paths,
        )

    if registry_service is not None:
        register_list_handler(
            router,
            registry_service=registry_service,
            external_discovery=external_discovery,
            external_binder=external_binder,
        )
        register_attach_handler(router, registry_service=registry_service)

    if external_discovery is not None and external_binder is not None:
        register_session_action_handlers(
            router,
            discovery=external_discovery,
            binder=external_binder,
        )

    if external_discovery is not None and external_binder is not None and structured_session_store is not None:
        register_external_session_handler(
            router,
            discovery=external_discovery,
            binder=external_binder,
            session_store=structured_session_store,
        )

    if (
        hook_socket_server is not None
        and unbound_permission_handler is not None
        and (permission_callback_registry is not None or permission_gateway is not None)
    ):
        register_external_permission_handler(
            router,
            hook_socket_server=hook_socket_server,
            unbound_permission_handler=unbound_permission_handler,
            permission_callback_registry=permission_callback_registry,
            external_uq_state=external_uq_state,
            auto_approve_service=auto_approve_service,
            permission_gateway=permission_gateway,
        )

    if file_receiver is not None and upload_queue is not None:
        register_file_upload_handler(
            router,
            file_receiver=file_receiver,
            session_service=session_service,
            task_service=task_service,
            upload_queue=upload_queue,
            upload_max_file_size_mb=settings.upload_max_file_size_mb,
            upload_queue_ttl_sec=settings.upload_queue_ttl_sec,
        )

    if result_exporter is not None:
        register_export_handler(
            router,
            task_service=task_service,
            result_exporter=result_exporter,
        )

    @router.message(F.text & ~F.text.startswith("/"))
    async def command_claude_chat_text(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        user_id = message.from_user.id if message.from_user else 0
        if await maybe_handle_pending_user_question_text(message=message, task_service=task_service):
            return
        session = await session_service.get(user_id)
        logger.info(
            "claude chat text received",
            extra={
                "user_id": user_id,
                "text_len": len(text),
                "has_session": session is not None,
                "claude_chat_active": bool(session and session.claude_chat_active),
                "session_provider": session.provider if session else None,
                "session_workdir": session.workdir if session else None,
                "session_claude_session_id": session.claude_session_id if session else None,
            },
        )
        if session is None or not session.claude_chat_active:
            await message.answer("请先发送 /claude")
            return

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
            permission_callback_registry=permission_callback_registry,
            permission_gateway=permission_gateway,
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

    return router
