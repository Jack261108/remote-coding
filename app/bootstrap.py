from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from app.adapters.claude.hook_installer import HookInstaller
from app.adapters.claude.hook_socket_server import HookSocketServer
from app.adapters.claude.paths import ClaudePaths
from app.domain.models import SessionContext
from app.adapters.cli.factory import CLIAdapterFactory
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.adapters.storage.file_session_store import FileSessionStore
from app.adapters.storage.memory import MemoryTaskStore
from app.bot.middleware.auth import AuthMiddleware
from app.bot.middleware.rate_limit import RateLimitMiddleware
from app.bot.router import create_router
from app.config.settings import Settings
from app.services.claude_jsonl_parser import ClaudeJSONLParser
from app.services.session_service import SessionService
from app.services.session_store import SessionStore
from app.domain.hook_models import HookEvent
from app.domain.session_models import SessionEvent, SessionEventType, SessionPhase
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


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

        self.runner = SubprocessRunner()
        self.claude_paths = ClaudePaths.resolve(settings.claude_config_dir)
        self.hook_installer = HookInstaller(
            paths=self.claude_paths,
            socket_path=settings.claude_hook_socket_path,
            claude_bin=settings.claude_cli_bin,
        )
        self.hook_socket_server = HookSocketServer(settings.claude_hook_socket_path)
        self.file_session_store = FileSessionStore(settings.tmux_data_dir)
        self.session_context_store = FileSessionContextStore(self.file_session_store)
        self.claude_jsonl_parser = ClaudeJSONLParser(self.claude_paths)
        self.structured_session_store = SessionStore(self.file_session_store)
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

        self.session_service = SessionService(store=self.session_context_store)
        self.task_service = TaskService(
            settings=settings,
            task_store=self.task_store,
            session_service=self.session_service,
            cli_factory=self.cli_factory,
            semaphore=asyncio.Semaphore(settings.max_concurrent_tasks),
            structured_session_store=self.structured_session_store,
            hook_socket_server=self.hook_socket_server,
        )
        self._jsonl_sync_tasks: dict[str, asyncio.Task[None]] = {}
        self._jsonl_sync_requests: dict[str, str] = {}
        self._jsonl_sync_locks: dict[str, asyncio.Lock] = {}
        self._periodic_recheck_task: asyncio.Task[None] | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        if self.settings.claude_install_hooks:
            self.hook_installer.install()
        await self.hook_socket_server.start(self._handle_hook_event, self._handle_permission_failure)
        await self._restore_session_bindings()
        self._periodic_recheck_task = asyncio.create_task(self._periodic_recheck_loop())
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            await self.bot.session.close()
            return
        await self._stop_periodic_recheck_task()
        await self._stop_jsonl_sync_tasks()
        await self.hook_socket_server.stop()
        await self.bot.session.close()
        self._started = False

    async def sync_claude_session(self, session_id: str, cwd: str) -> None:
        lock = self._jsonl_sync_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            snapshot = self.claude_jsonl_parser.parse_incremental(session_id=session_id, cwd=cwd)
            logger.info(
                "claude session synced",
                extra={
                    "session_id": session_id,
                    "cwd": cwd,
                    "turn_count": len(snapshot.turns),
                    "tool_call_count": len(snapshot.tool_calls),
                    "last_reply": snapshot.last_reply,
                    "last_reply_role": snapshot.last_reply_role,
                    "last_offset": snapshot.last_offset,
                    "clear_detected": snapshot.clear_detected,
                },
            )
            self.structured_session_store.process(
                SessionEvent(
                    session_id=session_id,
                    type=SessionEventType.FILE_SYNCED,
                    payload=snapshot.to_payload(),
                )
            )

    async def _stop_periodic_recheck_task(self) -> None:
        task = self._periodic_recheck_task
        self._periodic_recheck_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_jsonl_sync_tasks(self) -> None:
        tasks = list(self._jsonl_sync_tasks.values())
        self._jsonl_sync_tasks.clear()
        self._jsonl_sync_requests.clear()
        self._jsonl_sync_locks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def _periodic_recheck_loop(self) -> None:
        interval_sec = self.settings.claude_periodic_recheck_ms / 1000
        try:
            while True:
                await asyncio.sleep(interval_sec)
                await self._recheck_active_claude_sessions()
        except asyncio.CancelledError:
            raise

    async def _recheck_active_claude_sessions(self) -> None:
        sessions = await self.session_service.list_all()
        for session in sessions:
            if session.provider != "claude_code" or not session.claude_chat_active:
                continue
            if not session.claude_session_id:
                logger.info("periodic recheck skipped", extra={"user_id": session.user_id, "reason": "no_claude_session_id"})
                continue
            state = self.structured_session_store.get(session.claude_session_id)
            if state is None:
                logger.info(
                    "periodic recheck skipped",
                    extra={"user_id": session.user_id, "claude_session_id": session.claude_session_id, "reason": "no_state"},
                )
                continue
            if state.phase not in {SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_APPROVAL}:
                logger.info(
                    "periodic recheck skipped",
                    extra={
                        "user_id": session.user_id,
                        "claude_session_id": session.claude_session_id,
                        "reason": "phase_not_active",
                        "phase": state.phase.value,
                    },
                )
                continue
            logger.info(
                "periodic recheck syncing",
                extra={
                    "user_id": session.user_id,
                    "claude_session_id": session.claude_session_id,
                    "phase": state.phase.value,
                    "workdir": session.workdir,
                },
            )
            await self.sync_claude_session(session.claude_session_id, session.workdir)

    async def _restore_session_bindings(self) -> None:
        sessions = await self.session_service.list_all()
        for session in sessions:
            claude_session_id = session.claude_session_id
            if not claude_session_id:
                continue
            state = self.structured_session_store.get_or_create(
                session_id=claude_session_id,
                provider="claude_code",
                workdir=session.workdir,
                terminal_id=session.terminal_id,
                user_id=session.user_id,
            )
            if state.turns or state.tool_calls or state.pending_permission is not None:
                continue
            session_file = self.claude_jsonl_parser.session_file_path(session_id=claude_session_id, cwd=session.workdir)
            if session_file.exists():
                await self.sync_claude_session(claude_session_id, session.workdir)
                continue
            terminal_state = self.structured_session_store.find_by_terminal_id(session.terminal_id) if session.terminal_id else None
            if terminal_state is not None and terminal_state.phase in {SessionPhase.PROCESSING, SessionPhase.WAITING_FOR_APPROVAL}:
                continue
            await self.session_service.clear_claude_session(user_id=session.user_id)

    def _schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
        self._jsonl_sync_requests[session_id] = cwd
        existing = self._jsonl_sync_tasks.get(session_id)
        if existing is None or existing.done():
            self._jsonl_sync_tasks[session_id] = asyncio.create_task(self._debounced_sync_claude_session(session_id))

    async def _debounced_sync_claude_session(self, session_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.claude_jsonl_sync_debounce_ms / 1000)
                cwd = self._jsonl_sync_requests.get(session_id)
                if cwd is None:
                    return
                self._jsonl_sync_requests.pop(session_id, None)
                await self.sync_claude_session(session_id, cwd)
                if session_id not in self._jsonl_sync_requests:
                    return
        except asyncio.CancelledError:
            raise
        finally:
            current = self._jsonl_sync_tasks.get(session_id)
            if current is asyncio.current_task():
                self._jsonl_sync_tasks.pop(session_id, None)
                self._jsonl_sync_requests.pop(session_id, None)

    async def _handle_hook_event(self, event: HookEvent) -> None:
        logger.debug(
            "hook event received",
            extra={
                "session_id": event.session_id,
                "event": event.event,
                "status": event.status,
                "tool": event.tool,
            },
        )
        self.structured_session_store.get_or_create(
            session_id=event.session_id,
            provider="claude_code",
            workdir=event.cwd,
        )
        self.structured_session_store.process(
            SessionEvent(
                session_id=event.session_id,
                type=SessionEventType.HOOK_RECEIVED,
                payload=event.to_dict(),
            )
        )
        await self._bind_hook_session(event)
        self._schedule_jsonl_sync(event.session_id, event.cwd)

    async def _handle_permission_failure(self, session_id: str, tool_use_id: str) -> None:
        logger.warning(
            "permission response failed",
            extra={"session_id": session_id, "tool_use_id": tool_use_id},
        )
        self.structured_session_store.process(
            SessionEvent(
                session_id=session_id,
                type=SessionEventType.PERMISSION_RESPONSE_FAILED,
                payload={"tool_use_id": tool_use_id},
            )
        )

    async def _bind_hook_session(self, event: HookEvent) -> None:
        if not event.session_id:
            return
        matched = await self._match_session_context(event)
        logger.info(
            "hook session match result",
            extra={
                "hook_session_id": event.session_id,
                "hook_event": event.event,
                "hook_status": event.status,
                "hook_cwd": event.cwd,
                "matched_user_id": matched.user_id if matched is not None else None,
                "matched_workdir": matched.workdir if matched is not None else None,
                "matched_terminal_id": matched.terminal_id if matched is not None else None,
                "matched_claude_session_id": matched.claude_session_id if matched is not None else None,
            },
        )
        if matched is None:
            return
        await self.task_service.bind_claude_session(
            user_id=matched.user_id,
            claude_session_id=event.session_id,
            workdir=event.cwd or matched.workdir,
        )
        state = self.structured_session_store.get_or_create(
            session_id=event.session_id,
            provider="claude_code",
            workdir=event.cwd or matched.workdir,
            terminal_id=matched.terminal_id,
            user_id=matched.user_id,
        )
        state.terminal_id = matched.terminal_id
        state.user_id = matched.user_id
        self.structured_session_store._persist(state)

    async def _match_session_context(self, event: HookEvent) -> SessionContext | None:
        sessions = await self.session_service.list_all()
        logger.info(
            "matching hook session context",
            extra={
                "hook_session_id": event.session_id,
                "hook_cwd": event.cwd,
                "session_count": len(sessions),
            },
        )
        for session in sessions:
            if session.claude_session_id == event.session_id:
                logger.info(
                    "matched hook session by claude_session_id",
                    extra={
                        "hook_session_id": event.session_id,
                        "user_id": session.user_id,
                        "workdir": session.workdir,
                        "terminal_id": session.terminal_id,
                    },
                )
                return session
        event_workdir = str(Path(event.cwd).resolve()) if event.cwd else None
        workdir_matches: list[SessionContext] = []
        active_candidates: list[SessionContext] = []
        for session in sessions:
            session_workdir = str(Path(session.workdir).resolve()) if session.workdir else None
            logger.info(
                "evaluating hook session candidate",
                extra={
                    "hook_session_id": event.session_id,
                    "user_id": session.user_id,
                    "provider": session.provider,
                    "claude_chat_active": session.claude_chat_active,
                    "session_workdir": session.workdir,
                    "resolved_session_workdir": session_workdir,
                    "resolved_event_workdir": event_workdir,
                    "session_claude_session_id": session.claude_session_id,
                },
            )
            if session.provider != "claude_code" or not session.claude_chat_active:
                continue
            if event_workdir and session_workdir == event_workdir:
                workdir_matches.append(session)
                continue
            if event_workdir is None and session.claude_session_id is None:
                active_candidates.append(session)
        if len(workdir_matches) == 1:
            session = workdir_matches[0]
            logger.info(
                "matched hook session by workdir",
                extra={
                    "hook_session_id": event.session_id,
                    "user_id": session.user_id,
                    "resolved_event_workdir": event_workdir,
                    "resolved_session_workdir": str(Path(session.workdir).resolve()) if session.workdir else None,
                },
            )
            return session
        if event_workdir is None and len(active_candidates) == 1:
            session = active_candidates[0]
            logger.info(
                "matched hook session by single active candidate",
                extra={
                    "hook_session_id": event.session_id,
                    "user_id": session.user_id,
                    "workdir": session.workdir,
                },
            )
            return session
        logger.warning(
            "failed to match hook session context",
            extra={"hook_session_id": event.session_id, "hook_cwd": event.cwd},
        )
        return None

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
