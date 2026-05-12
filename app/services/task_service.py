from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
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
from app.services.session_store import SessionStore, is_claude_session_id
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
        self._structured_session_store = structured_session_store
        self._user_question_service = UserQuestionService(
            session_service=session_service,
            cli_factory=cli_factory,
            structured_session_store=structured_session_store,
            hook_socket_server=hook_socket_server,
            get_structured_session=self.get_structured_session,
            is_state_owned_by_user=self._is_state_owned_by_user,
        )
        self._permission_service = PermissionService(
            session_service=session_service,
            structured_session_store=structured_session_store,
            hook_socket_server=hook_socket_server,
            get_structured_session=self.get_structured_session,
            is_state_owned_by_user=self._is_state_owned_by_user,
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

        terminal_mode = selected_provider == "claude_code" and self._settings.claude_tmux_mode

        session = await self._session_service.get_or_create(
            user_id=user_id,
            provider=selected_provider,
            workdir=selected_workdir,
            terminal_mode=terminal_mode,
        )
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
        terminal_key = session.terminal_id if session.terminal_mode else None
        interactive = bool(
            terminal_key
            and record.provider == "claude_code"
            and session.claude_chat_active
            and self._settings.claude_tmux_mode
        )

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
        session = await self._session_service.get(user_id)
        if session is None:
            if log_missing:
                logger.info("structured session lookup failed", extra={"user_id": user_id, "reason": "no_session"})
            return None
        return self._lookup_structured_session(
            user_id=user_id,
            provider=session.provider,
            workdir=session.workdir,
            claude_session_id=session.claude_session_id,
            terminal_id=session.terminal_id,
            claude_chat_active=session.claude_chat_active,
            log_missing=log_missing,
        )

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True) -> SessionState | None:
        task = await self.get_status(task_id, user_id)
        if task is None:
            if log_missing:
                logger.info("structured session lookup failed", extra={"user_id": user_id, "task_id": task_id, "reason": "task_not_found"})
            return None
        session = await self._session_service.get(user_id)
        terminal_id = None
        claude_session_id = task.claude_session_id
        claude_chat_active = False
        if (
            session is not None
            and session.provider == "claude_code"
            and str(Path(session.workdir).resolve()) == str(Path(task.workdir).resolve())
        ):
            terminal_id = session.terminal_id
            claude_chat_active = session.claude_chat_active

        prompt_matched_state = None
        if self._structured_session_store is not None and terminal_id and task.prompt and task.started_at is not None:
            prompt_matched_state = self._structured_session_store.find_by_user_turn_text(
                user_id=user_id,
                workdir=task.workdir,
                text=task.prompt,
                since=task.started_at - timedelta(seconds=2),
                until=task.ended_at,
                terminal_id=terminal_id,
            )
        if prompt_matched_state is not None and self._is_claude_session_id(prompt_matched_state.claude_session_id or prompt_matched_state.session_id):
            if task.claude_session_id != prompt_matched_state.session_id:
                task.claude_session_id = prompt_matched_state.session_id
                await self._task_store.save(task)
            logger.info(
                "structured session lookup hit prompt turn",
                extra={
                    "user_id": user_id,
                    "task_id": task_id,
                    "session_id": prompt_matched_state.session_id,
                    "claude_session_id": prompt_matched_state.claude_session_id,
                    "turn_count": len(prompt_matched_state.turns),
                },
            )
            return prompt_matched_state

        return self._lookup_structured_session(
            user_id=user_id,
            provider=task.provider,
            workdir=task.workdir,
            claude_session_id=claude_session_id,
            terminal_id=terminal_id,
            claude_chat_active=claude_chat_active,
            log_missing=log_missing,
        )

    async def _get_structured_session_for_scope(self, *, user_id: int, task_id: str | None, log_missing: bool) -> SessionState | None:
        if task_id is not None:
            return await self.get_structured_session_for_task(task_id=task_id, user_id=user_id, log_missing=log_missing)
        return await self.get_structured_session(user_id, log_missing=log_missing)

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
        if provider != "claude_code":
            if log_missing:
                logger.info(
                    "structured session lookup failed",
                    extra={"user_id": user_id, "provider": provider, "workdir": workdir, "reason": "not_claude_provider"},
                )
            return None

        explicit_claude_session_id = claude_session_id if self._is_claude_session_id(claude_session_id) else None

        if self._structured_session_store is not None:
            state = None
            matched_by = None
            if explicit_claude_session_id is not None:
                state = self._structured_session_store.get(explicit_claude_session_id)
                matched_by = "claude_session_id"
                if state is not None and str(Path(state.workdir).resolve()) != str(Path(workdir).resolve()):
                    if log_missing:
                        logger.info(
                            "structured session lookup skipped",
                            extra={
                                "user_id": user_id,
                                "claude_session_id": explicit_claude_session_id,
                                "workdir": workdir,
                                "state_workdir": state.workdir,
                                "reason": "claude_session_workdir_mismatch",
                            },
                        )
                    state = None
                    matched_by = None
            if state is None and terminal_id:
                candidate = self._structured_session_store.find_by_terminal_id(terminal_id)
                if candidate is not None and self._is_claude_session_id(candidate.claude_session_id or candidate.session_id):
                    if str(Path(candidate.workdir).resolve()) == str(Path(workdir).resolve()):
                        state = candidate
                        matched_by = "terminal_id"
                    elif log_missing:
                        logger.info(
                            "structured session lookup skipped",
                            extra={
                                "user_id": user_id,
                                "terminal_id": terminal_id,
                                "workdir": workdir,
                                "state_workdir": candidate.workdir,
                                "reason": "terminal_workdir_mismatch",
                            },
                        )
            if state is not None:
                logger.info(
                    "structured session lookup hit store",
                    extra={
                        "user_id": user_id,
                        "matched_by": matched_by,
                        "claude_session_id": state.claude_session_id,
                        "session_id": state.session_id,
                        "phase": state.phase.value,
                        "turn_count": len(state.turns),
                    },
                )
                return state

        if explicit_claude_session_id is None:
            if log_missing:
                logger.info(
                    "structured session lookup failed",
                    extra={
                        "user_id": user_id,
                        "reason": "no_lookup_identity",
                        "provider": provider,
                        "workdir": workdir,
                        "terminal_id": terminal_id,
                        "claude_chat_active": claude_chat_active,
                    },
                )
            return None

        getter = getattr(self._cli_factory, "get_claude_session_state", None) or getattr(self._cli_factory, "get_session_state", None)
        if getter is None:
            if log_missing:
                logger.info(
                    "structured session lookup failed",
                    extra={"user_id": user_id, "claude_session_id": explicit_claude_session_id, "reason": "no_getter"},
                )
            return None
        state = getter(explicit_claude_session_id)
        if state is not None and str(Path(state.workdir).resolve()) != str(Path(workdir).resolve()):
            if log_missing:
                logger.info(
                    "structured session lookup skipped",
                    extra={
                        "user_id": user_id,
                        "claude_session_id": explicit_claude_session_id,
                        "workdir": workdir,
                        "state_workdir": state.workdir,
                        "reason": "fallback_workdir_mismatch",
                    },
                )
            return None
        logger.info(
            "structured session lookup fallback",
            extra={
                "user_id": user_id,
                "claude_session_id": explicit_claude_session_id,
                "state_found": state is not None,
                "phase": state.phase.value if state is not None else None,
                "turn_count": len(state.turns) if state is not None else 0,
            },
        )
        return state

    def _is_claude_session_id(self, session_id: str | None) -> bool:
        return is_claude_session_id(session_id)

    async def _is_state_owned_by_user(self, *, state: SessionState | None, user_id: int) -> bool:
        if state is None:
            return False
        if state.user_id is not None:
            return state.user_id == user_id

        session = await self._session_service.get(user_id)
        if session is None or session.provider != "claude_code":
            return False

        if session.claude_session_id:
            if state.session_id == session.claude_session_id or state.claude_session_id == session.claude_session_id:
                return True

        if session.terminal_id and state.terminal_id and session.terminal_id == state.terminal_id:
            session_workdir = str(Path(session.workdir).resolve()) if session.workdir else None
            state_workdir = str(Path(state.workdir).resolve()) if state.workdir else None
            return session_workdir == state_workdir

        return False

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        if self._structured_session_store is None:
            return 0
        state = await self._get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            return 0
        return self._structured_session_store.get_cursor(state.session_id)

    async def get_structured_session_revision(self, user_id: int) -> int:
        return await self.get_structured_session_cursor(user_id)

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None) -> tuple[str | None, str | None]:
        if self._structured_session_store is None:
            return None, None
        state = await self._get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            return None, None
        return self._structured_session_store.get_structured_reply_cursor(state.session_id)

    async def acknowledge_structured_reply(self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None, task_id: str | None = None) -> None:
        if self._structured_session_store is None:
            return
        state = await self._get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            return
        if turn_id is not None:
            self._structured_session_store.mark_structured_reply_emitted(state.session_id, turn_id=turn_id)
        if permission_key is not None:
            self._structured_session_store.mark_structured_permission_emitted(state.session_id, permission_key=permission_key)

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None) -> str | None:
        if task_id is not None and self._structured_session_store is not None:
            state = await self._get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
            if state is None:
                return None
            return self._structured_session_store.get_structured_user_question_cursor(state.session_id)
        return await self._user_question_service.get_structured_user_question_cursor(user_id)

    async def acknowledge_structured_user_question(self, user_id: int, *, question_key: str | None = None, task_id: str | None = None) -> None:
        if task_id is not None and self._structured_session_store is not None:
            state = await self._get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
            if state is None:
                return
            if question_key is not None:
                self._structured_session_store.mark_structured_user_question_emitted(state.session_id, question_key=question_key)
            return
        await self._user_question_service.acknowledge_structured_user_question(user_id, question_key=question_key)

    async def wait_for_structured_session_update(self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None) -> bool:
        if self._structured_session_store is None:
            await asyncio.sleep(timeout_sec)
            return True
        state = await self._get_structured_session_for_scope(user_id=user_id, task_id=task_id, log_missing=False)
        if state is None:
            await asyncio.sleep(timeout_sec)
            return True
        return await self._structured_session_store.wait_for_publish(state.session_id, since_cursor=since_cursor, timeout_sec=timeout_sec)

    async def wait_for_structured_session_change(self, *, user_id: int, since_revision: int, timeout_sec: float) -> bool:
        return await self.wait_for_structured_session_update(user_id=user_id, since_cursor=since_revision, timeout_sec=timeout_sec)

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
        session = await self._session_service.get(user_id)
        if session is None:
            return False, "当前无 session"
        if not session.terminal_mode or not session.terminal_id:
            if session.claude_chat_active:
                session.claude_session_id = None
                await self._session_service.clear_claude_session(user_id=user_id)
                await self._session_service.switch(user_id=user_id, claude_chat_active=False)
                self._user_question_service.clear_user(user_id)
                return True, "Claude 会话已退出"
            return False, "当前没有可关闭的持久终端"

        closed = await self._cli_factory.close_terminal(session.terminal_id)
        if not closed:
            return False, "终端不存在或关闭失败"

        session.claude_session_id = None
        await self._session_service.clear_claude_session(user_id=user_id)
        await self._session_service.switch(user_id=user_id, terminal_mode=False, claude_chat_active=False)
        self._user_question_service.clear_user(user_id)
        return True, "终端已关闭"

    async def open_claude_chat_session(self, user_id: int, *, workdir: str | None = None) -> tuple[bool, str]:
        session = await self._session_service.get(user_id)
        had_old_terminal = bool(session and session.terminal_mode and session.terminal_id)
        self._user_question_service.clear_user(user_id)
        if session is not None:
            await self._session_service.clear_claude_session(user_id=user_id)
        if had_old_terminal:
            closed, text = await self.close_terminal(user_id)
            if not closed and text != "终端不存在或关闭失败":
                return False, f"旧终端关闭失败: {text}"

        selected_workdir = str(Path(workdir or (session.workdir if session else self._settings.default_workdir)).resolve())
        if not self._is_workdir_allowed(selected_workdir):
            raise ValueError("workdir 不在 ALLOWED_WORKDIRS 白名单内")

        updated_session = await self._session_service.switch(
            user_id=user_id,
            provider="claude_code",
            workdir=selected_workdir,
            terminal_mode=True,
            claude_chat_active=True,
        )

        if not updated_session.terminal_id:
            return False, "会话创建失败: terminal_id 为空"

        ensure_result = await self._ensure_and_reveal_terminal(
            terminal_id=updated_session.terminal_id,
            workdir=updated_session.workdir,
            reveal=True,
            interactive=True,
        )
        if not ensure_result[0]:
            await self._session_service.switch(user_id=user_id, terminal_mode=False, claude_chat_active=False)
            return False, ensure_result[1]

        action = "Claude 会话已重建" if had_old_terminal else "Claude 会话已开启"
        message = action
        if ensure_result[1]:
            message = f"{message}\n{ensure_result[1]}"
        return True, message

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
        await self._session_service.bind_claude_session(
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
        if interactive:
            ensured, err = await self._cli_factory.ensure_claude_interactive_session(
                terminal_key=terminal_id,
                workdir=workdir,
            )
        else:
            ensured, err = await self._cli_factory.ensure_terminal(terminal_key=terminal_id, workdir=workdir)

        if not ensured:
            return False, err

        if not reveal:
            return True, ""

        revealed, reveal_text = await self._cli_factory.reveal_terminal(terminal_id)
        if revealed:
            return True, reveal_text
        return True, f"未能自动打开桌面终端: {reveal_text}"

    async def _apply_event(self, record: TaskRecord, event: CLIEvent) -> None:
        if event.type == EventType.STARTED:
            record.status = TaskStatus.RUNNING
            record.started_at = record.started_at or utc_now()
            return

        if event.type in {EventType.STDOUT, EventType.STDERR}:
            content = event.content or ""
            limit = self._settings.task_output_char_limit

            if record.output_chars >= limit:
                event.content = ""
                record.output_truncated = True
                return

            remaining = limit - record.output_chars
            if len(content) > remaining:
                event.content = content[:remaining]
                record.output_chars += remaining
                record.output_truncated = True
            else:
                record.output_chars += len(content)
            return

        record.ended_at = utc_now()

        if event.type == EventType.EXITED:
            record.status = TaskStatus.SUCCEEDED
            record.exit_code = event.exit_code
            record.failure_reason = None
        elif event.type == EventType.CANCELED:
            record.status = TaskStatus.CANCELED
            record.failure_reason = event.error
        elif event.type == EventType.TIMEOUT:
            record.status = TaskStatus.TIMEOUT
            record.failure_reason = event.error
        elif event.type == EventType.FAILED:
            record.status = TaskStatus.FAILED
            record.exit_code = event.exit_code
            record.failure_reason = event.error

        payload = {
            "task_id": record.task_id,
            "user_id": record.user_id,
            "provider": record.provider,
            "status": record.status.value,
            "duration_sec": record.duration_sec,
            "exit_code": record.exit_code,
            "failure_reason": record.failure_reason,
        }

        if record.status == TaskStatus.SUCCEEDED:
            logger.info("task completed", extra=payload)
        else:
            logger.error("task completed with error", extra=payload)

    def _is_workdir_allowed(self, workdir: str) -> bool:
        return is_workdir_allowed(workdir, self._settings.allowed_workdirs)
