from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from app.adapters.claude.hook_socket_server import HookSocketServer
from app.adapters.cli.factory import CLIAdapterFactory
from app.adapters.storage.memory import MemoryTaskStore
from app.config.settings import Settings, is_workdir_allowed
from app.domain.models import (
    CLIEvent,
    EventType,
    ExecutionTask,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from app.domain.session_models import SessionState
from app.domain.user_question_models import UserQuestionPrompt
from app.services.permission_service import PermissionService
from app.services.session_service import SessionService
from app.services.session_store import SessionStore
from app.services.structured_session_resolver import StructuredSessionResolver
from app.services.task_lifecycle_service import apply_task_event
from app.services.terminal_session_service import TerminalSessionService
from app.services.user_question_service import UserQuestionService

logger = logging.getLogger(__name__)


@dataclass
class StartTaskResult:
    task: TaskRecord
    events: AsyncIterator[CLIEvent]
    interactive: bool = False


class TaskService:
    def __init__(
        self,
        *,
        settings: Settings,
        task_store: MemoryTaskStore,
        session_service: SessionService,
        cli_factory: CLIAdapterFactory,
        semaphore: asyncio.Semaphore,
        structured_session_store: SessionStore | None = None,
        hook_socket_server: HookSocketServer | None = None,
    ) -> None:
        self._settings = settings
        self._task_store = task_store
        self._session_service = session_service
        self._cli_factory = cli_factory
        self._semaphore = semaphore
        self._structured_session_resolver = StructuredSessionResolver(
            session_service=session_service,
            task_store=task_store,
            cli_factory=cli_factory,
            structured_session_store=structured_session_store,
        )
        self._user_question_service = UserQuestionService(
            session_service=session_service,
            cli_factory=cli_factory,
            structured_session_store=structured_session_store,
            hook_socket_server=hook_socket_server,
            get_structured_session=self._structured_session_resolver.get_structured_session,
            is_state_owned_by_user=self._structured_session_resolver.is_state_owned_by_user,
        )
        self._permission_service = PermissionService(
            session_service=session_service,
            structured_session_store=structured_session_store,
            hook_socket_server=hook_socket_server,
            get_structured_session=self._structured_session_resolver.get_structured_session,
            is_state_owned_by_user=self._structured_session_resolver.is_state_owned_by_user,
        )
        self._terminal_session_service = TerminalSessionService(
            settings=settings,
            session_service=session_service,
            cli_factory=cli_factory,
            clear_user_questions=self._user_question_service.clear_user,
        )

    async def create_and_run(
        self,
        *,
        user_id: int,
        provider: str | None,
        prompt: str,
        timeout_sec: int | None = None,
        workdir: str | None = None,
    ) -> StartTaskResult:
        selected_provider = provider or self._settings.default_provider
        selected_provider = self._cli_factory.normalize_provider(selected_provider)

        selected_timeout = timeout_sec or self._settings.default_timeout_sec

        selected_workdir = str(Path(workdir or self._settings.default_workdir).resolve())
        if not self._is_workdir_allowed(selected_workdir):
            raise ValueError("workdir 不在 ALLOWED_WORKDIRS 白名单内")
        if not Path(selected_workdir).is_dir():
            raise ValueError(f"workdir 不存在或不是目录: {selected_workdir}")

        terminal_context = await self._terminal_session_service.resolve_for_task(
            user_id=user_id,
            provider=selected_provider,
            workdir=selected_workdir,
        )
        session = terminal_context.session
        logger.info(
            "session resolved",
            extra={
                "user_id": user_id,
                "provider": selected_provider,
                "terminal_mode": session.terminal_mode,
                "terminal_id": session.terminal_id,
            },
        )

        task_id = str(uuid.uuid4())
        record = TaskRecord(
            task_id=task_id,
            session_id=session.session_id,
            user_id=user_id,
            provider=selected_provider,
            prompt=prompt,
            workdir=selected_workdir,
            timeout_sec=selected_timeout,
            claude_session_id=session.claude_session_id,
        )
        await self._task_store.add(record)

        execution = ExecutionTask(
            task_id=record.task_id,
            session_id=record.session_id,
            user_id=record.user_id,
            provider=record.provider,
            prompt=record.prompt,
            workdir=record.workdir,
            timeout_sec=record.timeout_sec,
            claude_session_id=session.claude_session_id,
        )

        adapter = self._cli_factory.get(record.provider)
        terminal_key = terminal_context.terminal_key
        interactive = terminal_context.interactive

        if terminal_key:
            ensured, err = await self._ensure_and_reveal_terminal(
                terminal_id=terminal_key,
                workdir=record.workdir,
                reveal=False,
                interactive=interactive,
            )
            if not ensured:
                record.status = TaskStatus.FAILED
                record.ended_at = utc_now()
                record.failure_reason = err
                await self._task_store.save(record)
                raise ValueError(err)

        async def event_stream() -> AsyncIterator[CLIEvent]:
            async with self._semaphore:
                async for event in adapter.run(
                    execution,
                    terminal_key=terminal_key,
                    interactive=interactive,
                    claude_session_id=session.claude_session_id,
                ):
                    await self._apply_event(record, event)
                    await self._task_store.save(record)
                    yield event

        return StartTaskResult(task=record, events=event_stream(), interactive=interactive)

    async def cancel(self, task_id: str, user_id: int) -> bool:
        task = await self._task_store.get(task_id)
        if task is None or task.user_id != user_id:
            return False

        if task.is_final:
            return False

        adapter = self._cli_factory.get(task.provider)
        canceled = await adapter.cancel(task_id)
        if canceled:
            logger.info("task cancel requested", extra={"task_id": task_id, "user_id": user_id, "provider": task.provider})
        return canceled

    async def get_status(self, task_id: str, user_id: int) -> TaskRecord | None:
        task = await self._task_store.get(task_id)
        if task is None or task.user_id != user_id:
            return None
        return task

    async def list_recent(self, user_id: int, limit: int = 10) -> list[TaskRecord]:
        return await self._task_store.list_by_user(user_id=user_id, limit=limit)

    def available_providers(self) -> list[str]:
        return self._cli_factory.available_providers()

    def normalize_provider(self, provider: str) -> str:
        return self._cli_factory.normalize_provider(provider)

    def is_claude_tmux_enabled(self) -> bool:
        return self._settings.claude_tmux_mode

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True) -> SessionState | None:
        return await self._structured_session_resolver.get_structured_session(user_id, log_missing=log_missing)

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True) -> SessionState | None:
        return await self._structured_session_resolver.get_structured_session_for_task(
            task_id=task_id,
            user_id=user_id,
            log_missing=log_missing,
        )

    async def _get_structured_session_for_scope(self, *, user_id: int, task_id: str | None, log_missing: bool) -> SessionState | None:
        return await self._structured_session_resolver.get_structured_session_for_scope(
            user_id=user_id,
            task_id=task_id,
            log_missing=log_missing,
        )

    def _lookup_structured_session(
        self,
        *,
        user_id: int,
        provider: str,
        workdir: str,
        claude_session_id: str | None,
        terminal_id: str | None,
        claude_chat_active: bool,
        log_missing: bool,
    ) -> SessionState | None:
        return self._structured_session_resolver._lookup_structured_session(
            user_id=user_id,
            provider=provider,
            workdir=workdir,
            claude_session_id=claude_session_id,
            terminal_id=terminal_id,
            claude_chat_active=claude_chat_active,
            log_missing=log_missing,
        )

    def _is_claude_session_id(self, session_id: str | None) -> bool:
        return self._structured_session_resolver._is_claude_session_id(session_id)

    async def _is_state_owned_by_user(self, *, state: SessionState | None, user_id: int) -> bool:
        return await self._structured_session_resolver.is_state_owned_by_user(state=state, user_id=user_id)

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return await self._structured_session_resolver.get_structured_session_cursor(user_id, task_id=task_id)

    async def get_structured_session_revision(self, user_id: int) -> int:
        return await self._structured_session_resolver.get_structured_session_revision(user_id)

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None) -> tuple[str | None, str | None]:
        return await self._structured_session_resolver.get_structured_reply_cursor(user_id, task_id=task_id)

    async def acknowledge_structured_reply(
        self,
        user_id: int,
        *,
        turn_id: str | None = None,
        permission_key: str | None = None,
        task_id: str | None = None,
    ) -> None:
        await self._structured_session_resolver.acknowledge_structured_reply(
            user_id,
            turn_id=turn_id,
            permission_key=permission_key,
            task_id=task_id,
        )

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None) -> str | None:
        if task_id is not None:
            return await self._structured_session_resolver.get_structured_user_question_cursor_for_task(user_id, task_id=task_id)
        return await self._user_question_service.get_structured_user_question_cursor(user_id)

    async def acknowledge_structured_user_question(self, user_id: int, *, question_key: str | None = None, task_id: str | None = None) -> None:
        if task_id is not None:
            await self._structured_session_resolver.acknowledge_structured_user_question_for_task(
                user_id,
                question_key=question_key,
                task_id=task_id,
            )
            return
        await self._user_question_service.acknowledge_structured_user_question(user_id, question_key=question_key)

    async def wait_for_structured_session_update(self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None) -> bool:
        return await self._structured_session_resolver.wait_for_structured_session_update(
            user_id=user_id,
            since_cursor=since_cursor,
            timeout_sec=timeout_sec,
            task_id=task_id,
        )

    async def wait_for_structured_session_change(self, *, user_id: int, since_revision: int, timeout_sec: float) -> bool:
        return await self._structured_session_resolver.wait_for_structured_session_change(
            user_id=user_id,
            since_revision=since_revision,
            timeout_sec=timeout_sec,
        )

    async def get_pending_user_questions(self, user_id: int) -> tuple[UserQuestionPrompt, ...]:
        return await self._user_question_service.get_pending_user_questions(user_id)

    async def answer_pending_user_question_option(
        self,
        *,
        user_id: int,
        tool_use_id: str,
        question_index: int,
        option_index: int,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        return await self._user_question_service.answer_pending_user_question_option(
            user_id=user_id,
            tool_use_id=tool_use_id,
            question_index=question_index,
            option_index=option_index,
        )

    async def toggle_pending_user_question_multi_select_option(
        self,
        *,
        user_id: int,
        tool_use_id: str,
        question_index: int,
        option_index: int,
    ) -> tuple[bool, str, UserQuestionPrompt | None, frozenset[int] | None]:
        return await self._user_question_service.toggle_pending_user_question_multi_select_option(
            user_id=user_id,
            tool_use_id=tool_use_id,
            question_index=question_index,
            option_index=option_index,
        )

    async def submit_pending_user_question_multi_select(
        self,
        *,
        user_id: int,
        tool_use_id: str,
        question_index: int,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        return await self._user_question_service.submit_pending_user_question_multi_select(
            user_id=user_id,
            tool_use_id=tool_use_id,
            question_index=question_index,
        )

    async def answer_pending_user_question_text(
        self,
        *,
        user_id: int,
        text: str,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        return await self._user_question_service.answer_pending_user_question_text(user_id=user_id, text=text)

    async def close_terminal(self, user_id: int) -> tuple[bool, str]:
        return await self._terminal_session_service.close_terminal(user_id)

    async def open_claude_chat_session(self, user_id: int, *, workdir: str | None = None) -> tuple[bool, str]:
        return await self._terminal_session_service.open_claude_chat_session(user_id, workdir=workdir)

    def _extract_user_question_prompts_for_tool_use_id(
        self,
        state: SessionState | None,
        *,
        tool_use_id: str,
    ) -> tuple[UserQuestionPrompt, ...]:
        return self._user_question_service._extract_user_question_prompts_for_tool_use_id(
            state,
            tool_use_id=tool_use_id,
        )

    def _ensure_user_question_draft(self, *, user_id: int, prompts: tuple[UserQuestionPrompt, ...]) -> object:
        return self._user_question_service._ensure_user_question_draft(user_id=user_id, prompts=prompts)

    def is_workdir_allowed(self, workdir: str) -> bool:
        return self._is_workdir_allowed(str(Path(workdir).resolve()))

    async def bind_claude_session(self, *, user_id: int, claude_session_id: str, workdir: str | None = None) -> None:
        await self._terminal_session_service.bind_claude_session(
            user_id=user_id,
            claude_session_id=claude_session_id,
            workdir=workdir,
        )

    async def respond_to_pending_permission(
        self,
        *,
        user_id: int,
        decision: str,
        reason: str | None = None,
        expected_tool_use_id: str | None = None,
    ) -> tuple[bool, str]:
        return await self._permission_service.respond_to_pending_permission(
            user_id=user_id,
            decision=decision,
            reason=reason,
            expected_tool_use_id=expected_tool_use_id,
        )

    async def _ensure_and_reveal_terminal(
        self,
        *,
        terminal_id: str,
        workdir: str,
        reveal: bool,
        interactive: bool = False,
    ) -> tuple[bool, str]:
        return await self._terminal_session_service.ensure_and_reveal_terminal(
            terminal_id=terminal_id,
            workdir=workdir,
            reveal=reveal,
            interactive=interactive,
        )

    async def _apply_event(self, record: TaskRecord, event: CLIEvent) -> None:
        log_extra: dict[str, object] | None = None
        if event.type in {EventType.EXITED, EventType.CANCELED, EventType.TIMEOUT, EventType.FAILED}:
            session = await self._session_service.get(record.user_id)
            log_extra = {
                "session_id": record.session_id,
                "record_claude_session_id": record.claude_session_id,
                "session_terminal_mode": session.terminal_mode if session is not None else None,
                "session_terminal_id": session.terminal_id if session is not None else None,
                "session_claude_chat_active": session.claude_chat_active if session is not None else None,
                "session_claude_session_id": session.claude_session_id if session is not None else None,
                "interactive_like": bool(session and session.terminal_mode and session.claude_chat_active),
            }
        apply_task_event(
            record=record,
            event=event,
            output_char_limit=self._settings.task_output_char_limit,
            logger=logger,
            log_extra=log_extra,
        )

    def _is_workdir_allowed(self, workdir: str) -> bool:
        return is_workdir_allowed(workdir, self._settings.allowed_workdirs)
