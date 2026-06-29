from __future__ import annotations

from typing import TYPE_CHECKING

from app.adapters.claude.hook_installer import HookInstaller
from app.adapters.claude.hook_socket_server import HookSocketServer
from app.adapters.claude.paths import ClaudePaths
from app.adapters.cli.factory import CLIAdapterFactory
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.adapters.storage.file_session_store import FileSessionStore
from app.adapters.storage.memory import MemoryTaskStore
from app.config.settings import Settings
from app.infra.lock_registry import RefCountedLockRegistry
from app.services.admin_password_service import AdminPasswordService
from app.services.background_task_registry import BackgroundTaskRegistry
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.jsonl_file_watcher import JSONLFileWatcher
from app.services.risk_evaluator import RiskEvaluator
from app.services.session_registry import SessionRegistryService
from app.services.session_service import SessionService
from app.services.session_store import SessionStore
from app.services.session_supervisor import SessionSupervisor
from app.services.status_display import StatusDisplayService
from app.services.task_service import TaskService

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

    from app.bot.adapters.message_sender import AiogramMessageSender


class AppContainerBase:
    """Declares shared state attributes for AppContainer mixins."""

    settings: Settings
    bot: Bot
    dispatcher: Dispatcher
    task_store: MemoryTaskStore
    runner: SubprocessRunner
    claude_paths: ClaudePaths
    hook_installer: HookInstaller
    hook_socket_server: HookSocketServer
    file_session_store: FileSessionStore
    session_context_store: FileSessionContextStore
    claude_jsonl_parser: ClaudeJSONLParser
    structured_session_store: SessionStore
    jsonl_file_watcher: JSONLFileWatcher
    session_supervisor: SessionSupervisor
    admin_password_service: AdminPasswordService
    risk_evaluator: RiskEvaluator
    tmux_runner: TmuxRunner
    cli_factory: CLIAdapterFactory
    session_service: SessionService
    task_service: TaskService
    session_registry: SessionRegistryService
    _jsonl_sync_locks: RefCountedLockRegistry
    _session_event_locks: RefCountedLockRegistry
    _background_tasks: BackgroundTaskRegistry
    _started: bool
    message_sender: AiogramMessageSender
    status_display: StatusDisplayService
