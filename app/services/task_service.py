from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

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
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


@dataclass
class StartTaskResult:
    task: TaskRecord
    events: AsyncIterator[CLIEvent]


class TaskService:
    def __init__(
        self,
        *,
        settings: Settings,
        task_store: MemoryTaskStore,
        session_service: SessionService,
        cli_factory: CLIAdapterFactory,
        semaphore: asyncio.Semaphore,
    ) -> None:
        self._settings = settings
        self._task_store = task_store
        self._session_service = session_service
        self._cli_factory = cli_factory
        self._semaphore = semaphore

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
                async for event in adapter.run(execution, terminal_key=terminal_key, interactive=interactive):
                    await self._apply_event(record, event)
                    await self._task_store.save(record)
                    yield event

        return StartTaskResult(task=record, events=event_stream())

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

    async def close_terminal(self, user_id: int) -> tuple[bool, str]:
        session = await self._session_service.get(user_id)
        if session is None:
            return False, "当前无 session"
        if not session.terminal_mode or not session.terminal_id:
            if session.claude_chat_active:
                await self._session_service.switch(user_id=user_id, claude_chat_active=False)
                return True, "Claude 会话已退出"
            return False, "当前没有可关闭的持久终端"

        closed = await self._cli_factory.close_terminal(session.terminal_id)
        if not closed:
            return False, "终端不存在或关闭失败"

        await self._session_service.switch(user_id=user_id, terminal_mode=False, claude_chat_active=False)
        return True, "终端已关闭"

    async def open_claude_chat_session(self, user_id: int) -> tuple[bool, str]:
        session = await self._session_service.get(user_id)
        had_old_terminal = bool(session and session.terminal_mode and session.terminal_id)
        if had_old_terminal:
            closed, text = await self.close_terminal(user_id)
            if not closed:
                return False, f"旧终端关闭失败: {text}"

        selected_workdir = str(Path((session.workdir if session else self._settings.default_workdir)).resolve())
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
        tmux_session = f"tgcli_{updated_session.terminal_id}"[:64]
        return (
            True,
            f"{action}\ntmux_session: {tmux_session}\nterminal_id: {updated_session.terminal_id}\n{ensure_result[1]}",
        )

    def is_workdir_allowed(self, workdir: str) -> bool:
        return self._is_workdir_allowed(str(Path(workdir).resolve()))

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
