from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from app.adapters.claude.hook_socket_server import HookSocketServer
from app.adapters.cli.factory import CLIAdapterFactory
from app.adapters.storage.memory import MemoryTaskStore
from app.config.settings import Settings
from app.domain.models import (
    CLIEvent,
    EventType,
    ExecutionTask,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from app.domain.session_models import SessionEvent, SessionEventType, SessionState, ToolStatus
from app.domain.user_question_models import (
    UserQuestionPrompt,
    compose_user_question_answers,
    extract_user_question_prompts,
)
from app.services.session_service import SessionService
from app.services.session_store import SessionStore, is_claude_session_id

logger = logging.getLogger(__name__)


@dataclass
class StartTaskResult:
    task: TaskRecord
    events: AsyncIterator[CLIEvent]
    interactive: bool = False


@dataclass
class _UserQuestionDraft:
    tool_use_id: str
    prompts: tuple[UserQuestionPrompt, ...]
    answers_by_index: dict[int, str] = field(default_factory=dict)
    selected_option_indexes_by_question: dict[int, set[int]] = field(default_factory=dict)
    use_text_transport: bool = False


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
        self._hook_socket_server = hook_socket_server
        self._user_question_drafts: dict[int, _UserQuestionDraft] = {}

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
        if session is not None and session.provider == "claude_code" and session.workdir == task.workdir:
            terminal_id = session.terminal_id
            claude_session_id = claude_session_id or session.claude_session_id
            claude_chat_active = session.claude_chat_active
        return self._lookup_structured_session(
            user_id=user_id,
            provider=task.provider,
            workdir=task.workdir,
            claude_session_id=claude_session_id,
            terminal_id=terminal_id,
            claude_chat_active=claude_chat_active,
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
            if state is None and terminal_id:
                candidate = self._structured_session_store.find_by_terminal_id(terminal_id)
                if candidate is not None and self._is_claude_session_id(candidate.claude_session_id or candidate.session_id):
                    if candidate.workdir == workdir:
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

    async def get_structured_session_cursor(self, user_id: int) -> int:
        if self._structured_session_store is None:
            return 0
        state = await self.get_structured_session(user_id, log_missing=False)
        if state is None:
            return 0
        return self._structured_session_store.get_cursor(state.session_id)

    async def get_structured_session_revision(self, user_id: int) -> int:
        return await self.get_structured_session_cursor(user_id)

    async def get_structured_reply_cursor(self, user_id: int) -> tuple[str | None, str | None]:
        if self._structured_session_store is None:
            return None, None
        state = await self.get_structured_session(user_id, log_missing=False)
        if state is None:
            return None, None
        return self._structured_session_store.get_structured_reply_cursor(state.session_id)

    async def acknowledge_structured_reply(self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None) -> None:
        if self._structured_session_store is None:
            return
        state = await self.get_structured_session(user_id, log_missing=False)
        if state is None:
            return
        if turn_id is not None:
            self._structured_session_store.mark_structured_reply_emitted(state.session_id, turn_id=turn_id)
        if permission_key is not None:
            self._structured_session_store.mark_structured_permission_emitted(state.session_id, permission_key=permission_key)

    async def get_structured_user_question_cursor(self, user_id: int) -> str | None:
        if self._structured_session_store is None:
            return None
        state = await self.get_structured_session(user_id, log_missing=False)
        if state is not None:
            cursor = self._structured_session_store.get_structured_user_question_cursor(state.session_id)
            if cursor is not None:
                return cursor
        draft = self._user_question_drafts.get(user_id)
        if draft is not None:
            targeted_state = self._structured_session_store.find_by_active_user_question_tool_use_id(draft.tool_use_id)
            if targeted_state is not None:
                return self._structured_session_store.get_structured_user_question_cursor(targeted_state.session_id)
        return None

    async def acknowledge_structured_user_question(self, user_id: int, *, question_key: str | None = None) -> None:
        if self._structured_session_store is None or question_key is None:
            return
        state = self._structured_session_store.find_by_active_user_question_key(question_key)
        if state is None:
            draft = self._user_question_drafts.get(user_id)
            if draft is not None and draft.tool_use_id:
                state = self._structured_session_store.find_by_active_user_question_tool_use_id(draft.tool_use_id)
        if state is None:
            state = await self.get_structured_session(user_id, log_missing=False)
        if state is None:
            return
        self._structured_session_store.mark_structured_user_question_emitted(state.session_id, question_key=question_key)

    async def wait_for_structured_session_update(self, *, user_id: int, since_cursor: int, timeout_sec: float) -> bool:
        if self._structured_session_store is None:
            await asyncio.sleep(timeout_sec)
            return True
        state = await self.get_structured_session(user_id, log_missing=False)
        if state is None:
            await asyncio.sleep(timeout_sec)
            return True
        return await self._structured_session_store.wait_for_publish(state.session_id, since_cursor=since_cursor, timeout_sec=timeout_sec)

    async def wait_for_structured_session_change(self, *, user_id: int, since_revision: int, timeout_sec: float) -> bool:
        return await self.wait_for_structured_session_update(user_id=user_id, since_cursor=since_revision, timeout_sec=timeout_sec)

    async def get_pending_user_questions(self, user_id: int) -> tuple[UserQuestionPrompt, ...]:
        _, prompts = await self._resolve_active_user_question_context(user_id=user_id)
        self._sync_user_question_draft(user_id, prompts)
        return prompts

    async def answer_pending_user_question_option(
        self,
        *,
        user_id: int,
        tool_use_id: str,
        question_index: int,
        option_index: int,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        state, prompts = await self._resolve_active_user_question_context(
            user_id=user_id,
            expected_tool_use_id=tool_use_id,
        )
        if not prompts:
            return False, "当前没有待处理的选择题", None

        draft, prompt = self._locate_user_question_prompt(
            user_id=user_id,
            prompts=prompts,
            tool_use_id=tool_use_id,
            question_index=question_index,
        )
        if prompt is None:
            return False, "这个选择按钮已经过期，请等待最新的问题", None
        if option_index < 0 or option_index >= len(prompt.options):
            return False, "无效的选项", None
        if prompt.multi_select:
            return False, "这个问题需要先勾选，再点击提交选择", None

        answer = prompt.options[option_index].label
        return await self._submit_user_question_answer(
            user_id=user_id,
            draft=draft,
            prompt=prompt,
            answer=answer,
            state=state,
            selected_option_index=option_index,
        )

    async def toggle_pending_user_question_multi_select_option(
        self,
        *,
        user_id: int,
        tool_use_id: str,
        question_index: int,
        option_index: int,
    ) -> tuple[bool, str, UserQuestionPrompt | None, frozenset[int] | None]:
        state, prompts = await self._resolve_active_user_question_context(
            user_id=user_id,
            expected_tool_use_id=tool_use_id,
        )
        if not prompts:
            return False, "当前没有待处理的选择题", None, None

        draft, prompt = self._locate_user_question_prompt(
            user_id=user_id,
            prompts=prompts,
            tool_use_id=tool_use_id,
            question_index=question_index,
        )
        if prompt is None:
            return False, "这个选择按钮已经过期，请等待最新的问题", None, None
        if not prompt.multi_select:
            return False, "这个问题不是多选题", None, None
        if option_index < 0 or option_index >= len(prompt.options):
            return False, "无效的选项", None, None

        if not draft.use_text_transport:
            toggled, text = await self._toggle_user_question_option_in_terminal(
                user_id=user_id,
                state=state,
                prompt=prompt,
                option_index=option_index,
            )
            if not toggled:
                if self._is_user_question_text_transport_fallback_error(text):
                    draft.use_text_transport = True
                else:
                    return False, text, None, None

        selected_option_indexes = draft.selected_option_indexes_by_question.setdefault(question_index, set())
        if option_index in selected_option_indexes:
            selected_option_indexes.remove(option_index)
            message = f"已取消: {prompt.options[option_index].label}"
        else:
            selected_option_indexes.add(option_index)
            message = f"已选择: {prompt.options[option_index].label}"
        if not selected_option_indexes:
            draft.selected_option_indexes_by_question.pop(question_index, None)
        return True, message, prompt, frozenset(sorted(selected_option_indexes))

    async def submit_pending_user_question_multi_select(
        self,
        *,
        user_id: int,
        tool_use_id: str,
        question_index: int,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        state, prompts = await self._resolve_active_user_question_context(
            user_id=user_id,
            expected_tool_use_id=tool_use_id,
        )
        if not prompts:
            return False, "当前没有待处理的选择题", None

        draft, prompt = self._locate_user_question_prompt(
            user_id=user_id,
            prompts=prompts,
            tool_use_id=tool_use_id,
            question_index=question_index,
        )
        if prompt is None:
            return False, "这个选择按钮已经过期，请等待最新的问题", None
        if not prompt.multi_select:
            return False, "这个问题不是多选题", None

        selected_option_indexes = draft.selected_option_indexes_by_question.get(question_index, set())
        if not selected_option_indexes:
            return False, "请至少勾选一项再提交", None

        answer = "、".join(
            prompt.options[index].label
            for index in range(len(prompt.options))
            if index in selected_option_indexes
        )
        remaining = self._remaining_user_question_prompts(draft=draft, prompt=prompt)
        if not draft.use_text_transport:
            advanced, text = await self._advance_user_question_after_multi_select(
                user_id=user_id,
                state=state,
                final_question=not remaining,
            )
            if not advanced:
                if self._is_user_question_text_transport_fallback_error(text):
                    draft.use_text_transport = True
                else:
                    return False, text, None
        return await self._submit_user_question_answer(
            user_id=user_id,
            draft=draft,
            prompt=prompt,
            answer=answer,
            state=state,
            answer_applied_in_terminal=not draft.use_text_transport,
        )

    async def answer_pending_user_question_text(
        self,
        *,
        user_id: int,
        text: str,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        answer = text.strip()
        if not answer:
            return False, "回复内容不能为空", None

        state, prompts = await self._resolve_active_user_question_context(user_id=user_id)
        if not prompts:
            return False, "当前没有待处理的选择题", None

        draft = self._ensure_user_question_draft(user_id=user_id, prompts=prompts)
        prompt = next((item for item in prompts if item.question_index not in draft.answers_by_index), None)
        if prompt is None:
            return False, "当前没有待处理的选择题", None

        return await self._submit_user_question_answer(
            user_id=user_id,
            draft=draft,
            prompt=prompt,
            answer=answer,
            state=state,
        )

    async def close_terminal(self, user_id: int) -> tuple[bool, str]:
        session = await self._session_service.get(user_id)
        if session is None:
            return False, "当前无 session"
        if not session.terminal_mode or not session.terminal_id:
            if session.claude_chat_active:
                session.claude_session_id = None
                await self._session_service.clear_claude_session(user_id=user_id)
                await self._session_service.switch(user_id=user_id, claude_chat_active=False)
                self._user_question_drafts.pop(user_id, None)
                return True, "Claude 会话已退出"
            return False, "当前没有可关闭的持久终端"

        closed = await self._cli_factory.close_terminal(session.terminal_id)
        if not closed:
            return False, "终端不存在或关闭失败"

        session.claude_session_id = None
        await self._session_service.clear_claude_session(user_id=user_id)
        await self._session_service.switch(user_id=user_id, terminal_mode=False, claude_chat_active=False)
        self._user_question_drafts.pop(user_id, None)
        return True, "终端已关闭"

    async def open_claude_chat_session(self, user_id: int, *, workdir: str | None = None) -> tuple[bool, str]:
        session = await self._session_service.get(user_id)
        had_old_terminal = bool(session and session.terminal_mode and session.terminal_id)
        self._user_question_drafts.pop(user_id, None)
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

    def _extract_active_user_questions(self, state: SessionState | None) -> tuple[UserQuestionPrompt, ...]:
        if state is None:
            return ()

        pending = getattr(state, "pending_permission", None)
        if pending is not None:
            pending_prompts = extract_user_question_prompts(
                tool_use_id=pending.tool_use_id,
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
            )
            if pending_prompts:
                return pending_prompts

        waiting_prompts = self._extract_latest_user_question_prompts_from_tools(
            state,
            allowed_statuses={ToolStatus.WAITING_FOR_APPROVAL},
        )
        if waiting_prompts:
            return waiting_prompts

        return self._extract_latest_user_question_prompts_from_tools(
            state,
            allowed_statuses={ToolStatus.RUNNING},
        )

    def _extract_latest_user_question_prompts_from_tools(
        self,
        state: SessionState,
        *,
        allowed_statuses: set[ToolStatus],
    ) -> tuple[UserQuestionPrompt, ...]:
        latest_prompts: tuple[UserQuestionPrompt, ...] = ()
        latest_started_at = None
        for tool in state.tool_calls.values():
            if tool.status not in allowed_statuses:
                continue
            prompts = extract_user_question_prompts(
                tool_use_id=tool.tool_use_id,
                tool_name=tool.name,
                tool_input=tool.input,
            )
            if not prompts:
                continue
            if latest_started_at is None or tool.started_at >= latest_started_at:
                latest_started_at = tool.started_at
                latest_prompts = prompts
        return latest_prompts

    def _extract_user_question_prompts_for_tool_use_id(
        self,
        state: SessionState | None,
        *,
        tool_use_id: str,
    ) -> tuple[UserQuestionPrompt, ...]:
        if state is None or not tool_use_id:
            return ()

        pending = getattr(state, "pending_permission", None)
        if pending is not None and pending.tool_use_id == tool_use_id:
            pending_prompts = extract_user_question_prompts(
                tool_use_id=pending.tool_use_id,
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
            )
            if pending_prompts:
                return pending_prompts

        tool = state.tool_calls.get(tool_use_id)
        if tool is None or tool.status not in {ToolStatus.RUNNING, ToolStatus.WAITING_FOR_APPROVAL}:
            return ()
        return extract_user_question_prompts(
            tool_use_id=tool.tool_use_id,
            tool_name=tool.name,
            tool_input=tool.input,
        )

    def _sync_user_question_draft(self, user_id: int, prompts: tuple[UserQuestionPrompt, ...]) -> None:
        draft = self._user_question_drafts.get(user_id)
        if not prompts:
            self._user_question_drafts.pop(user_id, None)
            return
        if draft is None:
            return
        if draft.tool_use_id != prompts[0].tool_use_id:
            self._user_question_drafts.pop(user_id, None)
            return
        draft.prompts = prompts
        prompt_by_index = {prompt.question_index: prompt for prompt in prompts}
        draft.answers_by_index = {
            question_index: answer
            for question_index, answer in draft.answers_by_index.items()
            if question_index in prompt_by_index
        }
        filtered_selected_option_indexes_by_question: dict[int, set[int]] = {}
        for question_index, selected_option_indexes in draft.selected_option_indexes_by_question.items():
            prompt = prompt_by_index.get(question_index)
            if prompt is None or not prompt.multi_select:
                continue
            valid_selected_option_indexes = {
                option_index
                for option_index in selected_option_indexes
                if 0 <= option_index < len(prompt.options)
            }
            if valid_selected_option_indexes:
                filtered_selected_option_indexes_by_question[question_index] = valid_selected_option_indexes
        draft.selected_option_indexes_by_question = filtered_selected_option_indexes_by_question

    def _ensure_user_question_draft(self, *, user_id: int, prompts: tuple[UserQuestionPrompt, ...]) -> _UserQuestionDraft:
        draft = self._user_question_drafts.get(user_id)
        tool_use_id = prompts[0].tool_use_id
        if draft is None or draft.tool_use_id != tool_use_id:
            draft = _UserQuestionDraft(tool_use_id=tool_use_id, prompts=prompts)
            self._user_question_drafts[user_id] = draft
            return draft
        draft.prompts = prompts
        return draft

    def _locate_user_question_prompt(
        self,
        *,
        user_id: int,
        prompts: tuple[UserQuestionPrompt, ...],
        tool_use_id: str,
        question_index: int,
    ) -> tuple[_UserQuestionDraft, UserQuestionPrompt | None]:
        draft = self._ensure_user_question_draft(user_id=user_id, prompts=prompts)
        if not prompts or prompts[0].tool_use_id != tool_use_id:
            return draft, None
        prompt = next((item for item in prompts if item.question_index == question_index), None)
        return draft, prompt

    async def _resolve_active_user_question_context(
        self,
        *,
        user_id: int,
        expected_tool_use_id: str | None = None,
    ) -> tuple[SessionState | None, tuple[UserQuestionPrompt, ...]]:
        state = None
        current_state = None
        if expected_tool_use_id and self._structured_session_store is not None:
            state = self._structured_session_store.find_by_active_user_question_tool_use_id(expected_tool_use_id)
        if state is None:
            state = await self.get_structured_session(user_id, log_missing=False)
        current_state = state
        prompts = (
            self._extract_user_question_prompts_for_tool_use_id(state, tool_use_id=expected_tool_use_id)
            if expected_tool_use_id
            else self._extract_active_user_questions(state)
        )
        if not prompts and expected_tool_use_id and self._structured_session_store is not None:
            targeted_state = self._structured_session_store.find_by_active_user_question_tool_use_id(expected_tool_use_id)
            if targeted_state is not None and targeted_state is not state:
                state = targeted_state
                prompts = self._extract_user_question_prompts_for_tool_use_id(state, tool_use_id=expected_tool_use_id)
        if not prompts and expected_tool_use_id is not None:
            if current_state is None:
                current_state = await self.get_structured_session(user_id, log_missing=False)
            if current_state is not None and current_state is not state:
                current_prompts = self._extract_active_user_questions(current_state)
                if current_prompts:
                    state = current_state
                    prompts = current_prompts
            elif current_state is not None:
                prompts = self._extract_active_user_questions(current_state)
        if not prompts and expected_tool_use_id is None and self._structured_session_store is not None:
            draft = self._user_question_drafts.get(user_id)
            if draft is not None:
                draft_state = self._structured_session_store.find_by_active_user_question_tool_use_id(draft.tool_use_id)
                if draft_state is not None:
                    state = draft_state
                    prompts = self._extract_user_question_prompts_for_tool_use_id(state, tool_use_id=draft.tool_use_id)
        return state, prompts

    async def _submit_user_question_answer(
        self,
        *,
        user_id: int,
        draft: _UserQuestionDraft,
        prompt: UserQuestionPrompt,
        answer: str,
        state: SessionState | None = None,
        selected_option_index: int | None = None,
        answer_applied_in_terminal: bool = False,
    ) -> tuple[bool, str, UserQuestionPrompt | None]:
        normalized_answer = answer.strip()
        if not normalized_answer:
            return False, "回复内容不能为空", None

        remaining = self._remaining_user_question_prompts(draft=draft, prompt=prompt)
        if prompt.options and not answer_applied_in_terminal and not draft.use_text_transport:
            applied, text = await self._apply_user_question_answer_to_terminal(
                user_id=user_id,
                state=state,
                prompt=prompt,
                answer=normalized_answer,
                selected_option_index=selected_option_index,
                submit_after=not remaining,
            )
            if not applied:
                if self._is_user_question_text_transport_fallback_error(text):
                    draft.use_text_transport = True
                else:
                    return False, text, None

        draft.answers_by_index[prompt.question_index] = normalized_answer
        draft.selected_option_indexes_by_question.pop(prompt.question_index, None)
        if remaining:
            next_prompt = remaining[0]
            self._mark_user_question_prompt_emitted(state=state, prompt=next_prompt)
            return True, f"已记录选择: {normalized_answer}", next_prompt

        if prompt.options and not draft.use_text_transport:
            self._mark_user_question_completed(state=state, tool_use_id=prompt.tool_use_id)
            self._user_question_drafts.pop(user_id, None)
            return True, "已提交你的选择，Claude 继续执行中", None

        answer_text = compose_user_question_answers(draft.prompts, draft.answers_by_index)
        if not answer_text:
            return False, "答案内容为空，无法提交", None

        sent, text = await self._send_interactive_text(
            user_id=user_id,
            text=answer_text,
            terminal_id=state.terminal_id if state is not None else None,
            workdir=state.workdir if state is not None else None,
        )
        if sent:
            self._mark_user_question_completed(state=state, tool_use_id=prompt.tool_use_id)
            self._user_question_drafts.pop(user_id, None)
            return True, "已提交你的选择，Claude 继续执行中", None
        return False, text, None

    def _mark_user_question_completed(self, *, state: SessionState | None, tool_use_id: str) -> None:
        if self._structured_session_store is None or not tool_use_id:
            return

        target_state = self._structured_session_store.find_by_active_user_question_tool_use_id(tool_use_id)
        if target_state is None and state is not None:
            target_state = self._structured_session_store.get(state.session_id)
        if target_state is None:
            return

        self._structured_session_store.process(
            SessionEvent(
                session_id=target_state.session_id,
                type=SessionEventType.PERMISSION_APPROVED,
                payload={"tool_use_id": tool_use_id},
            )
        )

    def _mark_user_question_prompt_emitted(self, *, state: SessionState | None, prompt: UserQuestionPrompt) -> None:
        if self._structured_session_store is None:
            return

        target_state = self._structured_session_store.find_by_active_user_question_key(prompt.key)
        if target_state is None and prompt.tool_use_id:
            target_state = self._structured_session_store.find_by_active_user_question_tool_use_id(prompt.tool_use_id)
        if target_state is None and state is not None:
            target_state = self._structured_session_store.get(state.session_id)
        if target_state is None:
            return

        self._structured_session_store.mark_structured_user_question_emitted(
            target_state.session_id,
            question_key=prompt.key,
        )

    def _is_user_question_text_transport_fallback_error(self, text: str) -> bool:
        normalized = text.strip()
        return normalized.startswith("当前问题不是 Claude 选择框界面")

    def _remaining_user_question_prompts(
        self,
        *,
        draft: _UserQuestionDraft,
        prompt: UserQuestionPrompt,
    ) -> list[UserQuestionPrompt]:
        return [
            item
            for item in draft.prompts
            if item.question_index != prompt.question_index and item.question_index not in draft.answers_by_index
        ]

    async def _apply_user_question_answer_to_terminal(
        self,
        *,
        user_id: int,
        state: SessionState | None,
        prompt: UserQuestionPrompt,
        answer: str,
        selected_option_index: int | None,
        submit_after: bool,
    ) -> tuple[bool, str]:
        resolved_option_index = selected_option_index
        if resolved_option_index is None:
            resolved_option_index = next(
                (index for index, option in enumerate(prompt.options) if option.label == answer),
                None,
            )

        if resolved_option_index is not None:
            if prompt.multi_select:
                toggled, text = await self._toggle_user_question_option_in_terminal(
                    user_id=user_id,
                    state=state,
                    prompt=prompt,
                    option_index=resolved_option_index,
                )
                if not toggled:
                    return False, text
                return await self._advance_user_question_after_multi_select(
                    user_id=user_id,
                    state=state,
                    final_question=submit_after,
                )
            return await self._select_user_question_option_in_terminal(
                user_id=user_id,
                state=state,
                option_index=resolved_option_index,
                submit_after=submit_after,
            )

        return await self._answer_user_question_text_in_terminal(
            user_id=user_id,
            state=state,
            option_count=len(prompt.options),
            text=answer,
            submit_after=submit_after,
        )

    async def _resolve_user_question_terminal(
        self,
        *,
        user_id: int,
        state: SessionState | None,
    ) -> tuple[str | None, str | None, str | None]:
        if state is not None and state.terminal_id and state.workdir:
            return state.terminal_id, state.workdir, None

        session = await self._session_service.get(user_id)
        if session is None or session.provider != "claude_code":
            return None, None, "当前没有 Claude 会话"
        if not session.terminal_mode or not session.terminal_id:
            return None, None, "当前没有可用的 Claude 持久终端"
        return session.terminal_id, session.workdir, None

    async def _select_user_question_option_in_terminal(
        self,
        *,
        user_id: int,
        state: SessionState | None,
        option_index: int,
        submit_after: bool,
    ) -> tuple[bool, str]:
        terminal_id, workdir, err = await self._resolve_user_question_terminal(user_id=user_id, state=state)
        if err is not None or terminal_id is None or workdir is None:
            return False, err or "当前没有可用的 Claude 持久终端"
        sender = getattr(self._cli_factory, "select_claude_user_question_option", None)
        if sender is None:
            return False, "当前环境不支持直接操作选择题界面"
        return await sender(
            terminal_key=terminal_id,
            workdir=workdir,
            option_index=option_index,
            submit_after=submit_after,
        )

    async def _toggle_user_question_option_in_terminal(
        self,
        *,
        user_id: int,
        state: SessionState | None,
        prompt: UserQuestionPrompt,
        option_index: int,
    ) -> tuple[bool, str]:
        if not prompt.multi_select:
            return False, "这个问题不是多选题"
        return await self._select_user_question_option_in_terminal(
            user_id=user_id,
            state=state,
            option_index=option_index,
            submit_after=False,
        )

    async def _answer_user_question_text_in_terminal(
        self,
        *,
        user_id: int,
        state: SessionState | None,
        option_count: int,
        text: str,
        submit_after: bool,
    ) -> tuple[bool, str]:
        terminal_id, workdir, err = await self._resolve_user_question_terminal(user_id=user_id, state=state)
        if err is not None or terminal_id is None or workdir is None:
            return False, err or "当前没有可用的 Claude 持久终端"
        sender = getattr(self._cli_factory, "answer_claude_user_question_with_text", None)
        if sender is None:
            return False, "当前环境不支持直接在 Claude 选择题界面输入文字"
        return await sender(
            terminal_key=terminal_id,
            workdir=workdir,
            option_count=option_count,
            text=text,
            submit_after=submit_after,
        )

    async def _advance_user_question_after_multi_select(
        self,
        *,
        user_id: int,
        state: SessionState | None,
        final_question: bool,
    ) -> tuple[bool, str]:
        terminal_id, workdir, err = await self._resolve_user_question_terminal(user_id=user_id, state=state)
        if err is not None or terminal_id is None or workdir is None:
            return False, err or "当前没有可用的 Claude 持久终端"
        sender = getattr(self._cli_factory, "advance_claude_user_question_after_multi_select", None)
        if sender is None:
            return False, "当前环境不支持直接提交多选题"
        return await sender(
            terminal_key=terminal_id,
            workdir=workdir,
            final_question=final_question,
        )

    async def _send_interactive_text(
        self,
        *,
        user_id: int,
        text: str,
        terminal_id: str | None = None,
        workdir: str | None = None,
    ) -> tuple[bool, str]:
        session = await self._session_service.get(user_id)
        resolved_terminal_id = terminal_id
        resolved_workdir = workdir
        if resolved_terminal_id is None or resolved_workdir is None:
            if session is None or session.provider != "claude_code":
                return False, "当前没有 Claude 会话"
            if not session.terminal_mode or not session.terminal_id:
                return False, "当前没有可用的 Claude 持久终端"
            resolved_terminal_id = session.terminal_id
            resolved_workdir = session.workdir
        sender = getattr(self._cli_factory, "send_claude_interactive_input", None)
        if sender is None:
            return False, "当前环境不支持直接回复交互问题"
        return await sender(
            terminal_key=resolved_terminal_id,
            workdir=resolved_workdir,
            text=text,
        )

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
        session = await self._session_service.get(user_id)
        if session is None or session.provider != "claude_code":
            return False, "当前没有 Claude 会话"
        if self._structured_session_store is None or self._hook_socket_server is None:
            return False, "当前未启用 Claude hooks 权限通道"
        state = None
        if expected_tool_use_id is not None:
            state = self._structured_session_store.find_by_pending_tool_use_id(expected_tool_use_id)
        if state is None:
            state = await self.get_structured_session(user_id, log_missing=False)
        pending = state.pending_permission if state is not None else None
        if pending is None and expected_tool_use_id is None:
            return False, "当前没有待处理的权限请求"
        if expected_tool_use_id is not None and pending is not None and pending.tool_use_id != expected_tool_use_id:
            return False, "这个权限按钮已经过期，请等待最新的权限请求"
        tool_use_id = pending.tool_use_id if pending is not None else expected_tool_use_id
        if not tool_use_id:
            return False, "当前没有待处理的权限请求"
        sent = await self._hook_socket_server.respond_to_permission(tool_use_id=tool_use_id, decision=decision, reason=reason)
        if not sent:
            if pending is None:
                return False, "当前没有待处理的权限请求"
            return False, "待处理权限请求已失效，请等待 Claude 重新发起"
        tool_name = pending.tool_name if pending is not None else None
        if state is not None and pending is not None:
            event_type = SessionEventType.PERMISSION_APPROVED if decision == "allow" else SessionEventType.PERMISSION_DENIED
            updated = self._structured_session_store.process(
                SessionEvent(
                    session_id=state.session_id,
                    type=event_type,
                    payload={"tool_use_id": tool_use_id},
                )
            )
            tool_name = updated.last_tool_name or pending.tool_name
        action = "已批准" if decision == "allow" else "已拒绝"
        if tool_name:
            return True, f"{action}权限请求: {tool_name}"
        return True, f"{action}权限请求"

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
        target = Path(workdir).resolve()
        for allowed in self._settings.allowed_workdirs:
            allowed_path = Path(allowed).resolve()
            if target == allowed_path or allowed_path in target.parents:
                return True
        return False
