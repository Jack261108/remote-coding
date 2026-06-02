from __future__ import annotations

import asyncio
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
from app.services.agent_file_watcher import AgentFileWatcher
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.interrupt_watcher import InterruptWatcher
from app.services.lock_registry import RefCountedLockRegistry
from app.services.session_registry import SessionRegistryService
from app.services.session_service import SessionService
from app.services.session_store import SessionStore
from app.services.task_service import TaskService

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher


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
    interrupt_watcher: InterruptWatcher
    agent_file_watcher: AgentFileWatcher
    tmux_runner: TmuxRunner
    cli_factory: CLIAdapterFactory
    session_service: SessionService
    task_service: TaskService
    session_registry: SessionRegistryService
    _jsonl_sync_tasks: dict[str, asyncio.Task[None]]
    _jsonl_sync_requests: dict[str, str]
    _jsonl_sync_locks: RefCountedLockRegistry
    _session_event_locks: RefCountedLockRegistry
    _periodic_recheck_task: asyncio.Task[None] | None
    _background_tasks: set[asyncio.Task[None]]
    _started: bool
