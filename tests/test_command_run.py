from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup

from app.bot.handlers import command_run as command_run_module
from app.bot.handlers import run_event_streamer as run_event_streamer_module
from app.bot.handlers.command_run import _ABANDONED_STREAM_TASKS, _ACTIVE_STREAM_TASKS, run_prompt_and_stream
from app.bot.presenters.chunk_sender import ChunkSender
from app.bot.presenters.permission_message_builder import PermissionMessageBuilder, PermissionPromptInput
from app.bot.presenters.structured_reply_presenter import build_tool_progress_message, build_user_question_prompt
from app.bot.presenters.telegram_formatting import render_markdownish_to_telegram_html
from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus, utc_now
from app.domain.session_models import ConversationTurn, PendingPermission, SessionPhase, SubagentToolCall, ToolCallRecord, ToolStatus
from app.services.message_sender import Button, Keyboard
from app.services.permission_callback_registry import AutoApproveOutcome, SessionOrigin
from app.services.permission_gateway import RegisterForButtonOk
from tests.fakes.structured import make_structured_session as _structured_session
from tests.fakes.telegram import DummyMessage


class DummyTaskService:
    def __init__(
        self,
        events: list[CLIEvent],
        status: TaskRecord | None = None,
        *,
        interactive: bool = False,
        structured_reply: str = "",
        structured_turns: list[ConversationTurn] | None = None,
        structured_sessions: list[object | None] | None = None,
        event_delays: list[float] | None = None,
    ) -> None:
        self._events = events
        self._status = status
        self._interactive = interactive
        self._structured_reply = structured_reply
        self._structured_turns = structured_turns
        self._structured_sessions = structured_sessions
        self._structured_session_index = 0
        self._event_delays = event_delays or [0.0] * len(events)
        self._revision = 0
        self._structured_reply_turn_id: str | None = None
        self._structured_permission_key: str | None = None
        self._structured_user_question_key: str | None = None
        self.create_calls: list[tuple[int, str | None, str, str | None]] = []

    async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
        self.create_calls.append((user_id, provider, prompt, workdir))
        task = SimpleNamespace(
            task_id="t1",
            provider="claude_code",
            session_id="s1",
            workdir=workdir or "/tmp",
            started_at=None,
            created_at="2025-01-01T00:00:00Z",
        )
        return SimpleNamespace(task=task, events=self._stream(), interactive=self._interactive)

    async def get_status(self, task_id: str, user_id: int):
        return self._status

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        if self._structured_sessions is not None:
            if self._structured_session_index < len(self._structured_sessions):
                session = self._structured_sessions[self._structured_session_index]
                self._structured_session_index += 1
            else:
                session = self._structured_sessions[-1]
            self._revision += 1
            return session
        if self._structured_turns is not None:
            return SimpleNamespace(
                session_id="claude-session-1",
                phase=SessionPhase.WAITING_FOR_INPUT,
                turns=self._structured_turns,
                pending_permission=None,
            )
        if not self._structured_reply:
            return None
        return SimpleNamespace(
            session_id="claude-session-1",
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[ConversationTurn(turn_id="turn-1", role="assistant", text=self._structured_reply, is_complete=True)],
            pending_permission=None,
        )

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True):
        return await self.get_structured_session(user_id, log_missing=log_missing)

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return self._revision

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        return self._structured_reply_turn_id, self._structured_permission_key

    async def acknowledge_structured_reply(
        self, user_id: int, *, turn_id: str | None = None, permission_key: str | None = None, task_id: str | None = None
    ) -> None:
        if turn_id is not None:
            self._structured_reply_turn_id = turn_id
        if permission_key is not None:
            self._structured_permission_key = permission_key

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None):
        return self._structured_user_question_key

    async def acknowledge_structured_user_question(
        self, user_id: int, *, question_key: str | None = None, task_id: str | None = None
    ) -> None:
        self._structured_user_question_key = question_key

    async def wait_for_structured_session_update(
        self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None
    ) -> bool:
        await asyncio.sleep(timeout_sec)
        return True

    async def _stream(self):
        for delay, event in zip(self._event_delays, self._events, strict=False):
            if delay > 0:
                await asyncio.sleep(delay)
            yield event


async def _run_and_wait(
    *,
    message: DummyMessage,
    task_service: DummyTaskService,
    wait_sec: float = 0.05,
    permission_gateway: object | None = None,
) -> None:
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        permission_gateway=permission_gateway,
    )
    await asyncio.sleep(wait_sec)
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_run_prompt_waits_for_pending_upload_finalizer_before_create_task() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )
    finalizer_started = asyncio.Event()
    release_finalizer = asyncio.Event()

    async def finalizer(message_arg: DummyMessage, user_id: int) -> None:
        assert message_arg is message
        assert user_id == message.from_user.id
        finalizer_started.set()
        await release_finalizer.wait()

    outer_task = asyncio.create_task(
        run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
            user_id=message.from_user.id,
            provider="claude_code",
            prompt="hello",
            workdir="/tmp",
            pending_upload_finalizer=finalizer,
        )
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert task_service.create_calls == []

    release_finalizer.set()
    stream_task = await outer_task
    assert task_service.create_calls == [(message.from_user.id, "claude_code", "hello", "/tmp")]
    if stream_task is not None:
        await stream_task


class FakeRegistry:
    async def update_telegram_message(self, token: str, chat_id: int, message_id: int, message_text: str | None = None) -> bool:
        return True


class FakePermissionGateway:
    def __init__(self) -> None:
        self.message_builder = PermissionMessageBuilder()
        self.registry = FakeRegistry()
        self.registrations: list[tuple[str, str, SessionOrigin, int | None]] = []
        self.auto_approve_calls: list[tuple[str, SessionOrigin, int | None, str]] = []
        self.keyboard = Keyboard(
            rows=[
                [
                    Button(text="✅ Approve", callback_data="perm:tok12345:allow"),
                    Button(text="❌ Deny", callback_data="perm:tok12345:deny"),
                ],
                [Button(text="🟢 Auto-approve", callback_data="perm:tok12345:auto_approve")],
            ]
        )

    async def maybe_auto_approve(
        self,
        *,
        session_id: str,
        origin: SessionOrigin,
        candidate_user_id: int | None,
        tool_use_id: str,
        tool_name: str,
        tool_input: object,
    ) -> AutoApproveOutcome:
        del tool_input
        self.auto_approve_calls.append((session_id, origin, candidate_user_id, tool_use_id))
        return AutoApproveOutcome.NOT_APPROVED

    async def register_for_button(
        self,
        *,
        tool_use_id: str,
        session_id: str,
        origin: SessionOrigin,
        candidate_user_id: int | None,
    ) -> RegisterForButtonOk:
        self.registrations.append((tool_use_id, session_id, origin, candidate_user_id))
        return RegisterForButtonOk(keyboard=self.keyboard, token="test-token")


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_pump_silences_missing_structured_logs() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
    )
    task_service.get_structured_session = AsyncMock(return_value=None)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.12)

    assert task_service.get_structured_session.await_count >= 2
    initial_call = task_service.get_structured_session.await_args_list[0]
    repeated_calls = task_service.get_structured_session.await_args_list[1:]
    assert initial_call.kwargs == {"log_missing": True}
    assert repeated_calls
    assert any(call.kwargs.get("log_missing") is False for call in repeated_calls)
    assert repeated_calls[-1].kwargs == {"log_missing": True}


def _status(*, task_status: TaskStatus, truncated: bool = False) -> TaskRecord:
    started_at = utc_now() - timedelta(seconds=2)
    ended_at = utc_now()
    return TaskRecord(
        task_id="t1",
        session_id="s1",
        user_id=1,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        timeout_sec=30,
        status=task_status,
        started_at=started_at,
        ended_at=ended_at,
        output_truncated=truncated,
    )


@pytest.mark.asyncio
async def test_run_prompt_and_stream_keeps_background_task_referenced_until_done() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        event_delays=[0.0, 0.03],
    )

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    assert task in _ACTIVE_STREAM_TASKS
    await task
    assert task not in _ACTIVE_STREAM_TASKS


@pytest.mark.asyncio
async def test_run_prompt_and_stream_watchdog_cancels_stuck_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_BUFFER_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_MIN_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)

    class StuckTaskService(DummyTaskService):
        def __init__(self) -> None:
            super().__init__([], _status(task_status=TaskStatus.RUNNING))
            self.cancel_called = False
            self.timeout_marked = False

        async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
            async def stream():
                yield CLIEvent(type=EventType.STARTED, task_id="t-stuck")
                await asyncio.Event().wait()

            task = _status(task_status=TaskStatus.RUNNING)
            task.task_id = "t-stuck"
            task.timeout_sec = 0.01
            return SimpleNamespace(task=task, events=stream(), interactive=False)

        async def cancel(self, task_id: str, user_id: int) -> bool:
            self.cancel_called = True
            return True

        async def mark_stream_timeout(self, task_id: str, user_id: int, *, reason: str) -> bool:
            self.timeout_marked = True
            self._status.status = TaskStatus.TIMEOUT
            return True

    message = DummyMessage()
    task_service = StuckTaskService()
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    try:
        await asyncio.sleep(0.2)
        assert task.done()
        assert task not in _ACTIVE_STREAM_TASKS
        assert task_service.cancel_called is True
        assert task_service.timeout_marked is True
        assert task_service._status.status == TaskStatus.TIMEOUT
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_watchdog_timeout_schedules_queued_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_BUFFER_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_MIN_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    upload_scheduled = False

    class StuckTaskService(DummyTaskService):
        def __init__(self) -> None:
            super().__init__([], _status(task_status=TaskStatus.RUNNING))

        async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
            async def stream():
                yield CLIEvent(type=EventType.STARTED, task_id="t-timeout-upload")
                await asyncio.Event().wait()

            task = _status(task_status=TaskStatus.RUNNING)
            task.task_id = "t-timeout-upload"
            task.timeout_sec = 0.01
            return SimpleNamespace(task=task, events=stream(), interactive=False)

        async def cancel(self, task_id: str, user_id: int) -> bool:
            return True

        async def mark_stream_timeout(self, task_id: str, user_id: int, *, reason: str) -> bool:
            self._status.status = TaskStatus.TIMEOUT
            self._status.failure_reason = reason
            return True

    def queued_upload_scheduler(message: DummyMessage, user_id: int, task_id: str) -> None:
        nonlocal upload_scheduled
        upload_scheduled = True

    message = DummyMessage()
    task = await run_prompt_and_stream(
        message=message,
        task_service=StuckTaskService(),
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        queued_upload_scheduler=queued_upload_scheduler,
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert upload_scheduled is True
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_watchdog_ignores_late_exit_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_BUFFER_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_MIN_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CANCEL_GRACE_SEC", 0.005, raising=False)

    class LateExitTaskService(DummyTaskService):
        def __init__(self) -> None:
            super().__init__([], _status(task_status=TaskStatus.RUNNING))

        async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
            async def stream():
                yield CLIEvent(type=EventType.STARTED, task_id="t-late")
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await asyncio.sleep(0.02)
                    yield CLIEvent(type=EventType.EXITED, task_id="t-late", exit_code=0)

            task = _status(task_status=TaskStatus.RUNNING)
            task.task_id = "t-late"
            task.timeout_sec = 0.01
            return SimpleNamespace(task=task, events=stream(), interactive=False)

        async def cancel(self, task_id: str, user_id: int) -> bool:
            return True

        async def mark_stream_timeout(self, task_id: str, user_id: int, *, reason: str) -> bool:
            self._status.status = TaskStatus.TIMEOUT
            self._status.failure_reason = reason
            return True

    message = DummyMessage()
    task_service = LateExitTaskService()
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert task_service._status.status == TaskStatus.TIMEOUT
        lifecycle = message.sent_messages[0]
        assert not any("✅ 完成 [t-late]" in edit for edit in lifecycle.edits)
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_marks_timeout_before_cancel_race(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_BUFFER_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_MIN_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    cancel_released = asyncio.Event()

    class CancelRaceTaskService(DummyTaskService):
        def __init__(self) -> None:
            super().__init__([], _status(task_status=TaskStatus.RUNNING))

        async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
            async def stream():
                yield CLIEvent(type=EventType.STARTED, task_id="t-race")
                await cancel_released.wait()
                if self._status.status == TaskStatus.RUNNING:
                    self._status.status = TaskStatus.SUCCEEDED
                yield CLIEvent(type=EventType.EXITED, task_id="t-race", exit_code=0)

            task = _status(task_status=TaskStatus.RUNNING)
            task.task_id = "t-race"
            task.timeout_sec = 0.01
            return SimpleNamespace(task=task, events=stream(), interactive=False)

        async def cancel(self, task_id: str, user_id: int) -> bool:
            cancel_released.set()
            await asyncio.sleep(0.02)
            return True

        async def mark_stream_timeout(self, task_id: str, user_id: int, *, reason: str) -> bool:
            if self._status.status != TaskStatus.RUNNING:
                return False
            self._status.status = TaskStatus.TIMEOUT
            self._status.failure_reason = reason
            return True

    message = DummyMessage()
    task_service = CancelRaceTaskService()
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert task_service._status.status == TaskStatus.TIMEOUT
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_does_not_abandon_terminal_event_when_timeout_mark_loses_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_BUFFER_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_MIN_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    terminal_released = asyncio.Event()

    class TerminalRaceTaskService(DummyTaskService):
        def __init__(self) -> None:
            super().__init__([], _status(task_status=TaskStatus.RUNNING))
            self.cancel_called = False

        async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
            async def stream():
                yield CLIEvent(type=EventType.STARTED, task_id="t-terminal-race")
                await terminal_released.wait()
                self._status.status = TaskStatus.SUCCEEDED
                self._status.exit_code = 0
                yield CLIEvent(type=EventType.EXITED, task_id="t-terminal-race", exit_code=0)

            task = _status(task_status=TaskStatus.RUNNING)
            task.task_id = "t-terminal-race"
            task.timeout_sec = 0.01
            return SimpleNamespace(task=task, events=stream(), interactive=False)

        async def cancel(self, task_id: str, user_id: int) -> bool:
            self.cancel_called = True
            return True

        async def mark_stream_timeout(self, task_id: str, user_id: int, *, reason: str) -> bool:
            terminal_released.set()
            await asyncio.sleep(0.02)
            if self._status.status != TaskStatus.RUNNING:
                return False
            self._status.status = TaskStatus.TIMEOUT
            self._status.failure_reason = reason
            return True

    message = DummyMessage()
    task_service = TerminalRaceTaskService()
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert task_service._status.status == TaskStatus.SUCCEEDED
        assert task_service.cancel_called is False
        assert not any("任务流处理超时" in answer for answer in message.answers)
        lifecycle = message.sent_messages[0]
        assert any("✅ 完成 [t-termin]" in edit for edit in lifecycle.edits)
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_watchdog_allows_interactive_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_BUFFER_SEC", 0.03, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_MIN_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)

    class ProgressTaskService(DummyTaskService):
        async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
            async def stream():
                yield CLIEvent(type=EventType.STARTED, task_id="t-progress")
                for index in range(3):
                    await asyncio.sleep(0.02)
                    yield CLIEvent(type=EventType.STDOUT, task_id="t-progress", content=f"progress {index}\n")
                yield CLIEvent(type=EventType.EXITED, task_id="t-progress", exit_code=0)

            task = _status(task_status=TaskStatus.SUCCEEDED)
            task.task_id = "t-progress"
            task.timeout_sec = 0.01
            return SimpleNamespace(task=task, events=stream(), interactive=True)

    message = DummyMessage()
    task = await run_prompt_and_stream(
        message=message,
        task_service=ProgressTaskService([], _status(task_status=TaskStatus.SUCCEEDED), interactive=True),
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    await task

    lifecycle = message.sent_messages[0]
    assert any("✅ 完成 [t-progre]" in edit for edit in lifecycle.edits)
    assert not any("任务流处理超时" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_watchdog_allows_structured_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_BUFFER_SEC", 0.03, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_MIN_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)

    class StructuredProgressTaskService(DummyTaskService):
        def __init__(self) -> None:
            super().__init__([], _status(task_status=TaskStatus.SUCCEEDED), interactive=True)
            self._cursor = 0

        async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
            async def stream():
                yield CLIEvent(type=EventType.STARTED, task_id="t-structured")
                await asyncio.sleep(0.06)
                yield CLIEvent(type=EventType.EXITED, task_id="t-structured", exit_code=0)

            task = _status(task_status=TaskStatus.SUCCEEDED)
            task.task_id = "t-structured"
            task.timeout_sec = 0.01
            return SimpleNamespace(task=task, events=stream(), interactive=True)

        async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
            self._cursor += 1
            return self._cursor

    message = DummyMessage()
    task = await run_prompt_and_stream(
        message=message,
        task_service=StructuredProgressTaskService(),
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    await task

    lifecycle = message.sent_messages[0]
    assert any("✅ 完成 [t-struct]" in edit for edit in lifecycle.edits)
    assert not any("任务流处理超时" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_reports_create_errors() -> None:
    class FailingTaskService(DummyTaskService):
        async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
            raise RuntimeError("boom")

    message = DummyMessage()

    task = await run_prompt_and_stream(
        message=message,
        task_service=FailingTaskService([]),
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is None
    assert message.answers == ["创建任务失败，请稍后重试"]
    assert "boom" not in message.answers[0]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_passes_timing_settings_to_streamer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    original_init = command_run_module.RunEventStreamer.__init__

    def capture_init(self, *args, **kwargs):
        captured["structured_reply_pump_interval_sec"] = kwargs["structured_reply_pump_interval_sec"]
        captured["spinner_initial_delay_sec"] = kwargs["spinner_initial_delay_sec"]
        captured["spinner_interval_sec"] = kwargs["spinner_interval_sec"]
        captured["status_display"] = kwargs["status_display"]
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(command_run_module.RunEventStreamer, "__init__", capture_init)
    status_display = SimpleNamespace(start=AsyncMock(), clear=AsyncMock(), update_for_tool=AsyncMock())
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        status_display=status_display,
        structured_reply_pump_interval_sec=0.77,
        spinner_initial_delay_sec=0.88,
        spinner_interval_sec=0.99,
    )

    assert task is not None
    await task
    assert captured == {
        "structured_reply_pump_interval_sec": 0.77,
        "spinner_initial_delay_sec": 0.88,
        "spinner_interval_sec": 0.99,
        "status_display": status_display,
    }
    lifecycle = message.sent_messages[0]
    assert lifecycle.deleted is True
    assert not any("✅ 完成" in edit for edit in lifecycle.edits)
    status_display.clear.assert_awaited()


@pytest.mark.asyncio
async def test_run_prompt_and_stream_watchdog_cancels_stuck_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_FINALIZE_GRACE_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CANCEL_GRACE_SEC", 0.01, raising=False)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def stuck_after_terminal(self):
        try:
            async for event in self._start.events:
                if event.type == EventType.EXITED:
                    await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release_cleanup.wait()

    monkeypatch.setattr(command_run_module.RunEventStreamer, "stream_events", stuck_after_terminal)

    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t-final"),
            CLIEvent(type=EventType.EXITED, task_id="t-final", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert task not in _ACTIVE_STREAM_TASKS
        assert cleanup_started.is_set()
    finally:
        release_cleanup.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_tracks_abandoned_stream_and_force_cleans_interactive_pump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_FINALIZE_GRACE_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CANCEL_GRACE_SEC", 0.01, raising=False)
    pump_task: asyncio.Task | None = None
    inner_stream_task: asyncio.Task | None = None
    release_stream = asyncio.Event()

    async def stuck_after_terminal(self):
        nonlocal pump_task, inner_stream_task
        inner_stream_task = asyncio.current_task()
        pump_task = asyncio.create_task(asyncio.Event().wait())
        self._interactive_pump = pump_task
        try:
            async for event in self._start.events:
                if event.type == EventType.EXITED:
                    await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_stream.wait()

    monkeypatch.setattr(command_run_module.RunEventStreamer, "stream_events", stuck_after_terminal)

    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t-pump-cleanup"),
            CLIEvent(type=EventType.EXITED, task_id="t-pump-cleanup", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
    )
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert task not in _ACTIVE_STREAM_TASKS
        assert inner_stream_task is not None
        assert inner_stream_task in _ABANDONED_STREAM_TASKS
        assert pump_task is not None
        assert pump_task.cancelled()
    finally:
        release_stream.set()
        if inner_stream_task is not None and not inner_stream_task.done():
            await inner_stream_task
        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
            with suppress(asyncio.CancelledError):
                await pump_task
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_force_cleanup_does_not_block_on_uncancellable_interactive_pump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_FINALIZE_GRACE_SEC", 0.01, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CANCEL_GRACE_SEC", 0.01, raising=False)
    pump_task: asyncio.Task | None = None
    release_pump = asyncio.Event()
    release_stream = asyncio.Event()

    async def uncancellable_pump():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_pump.wait()

    async def stuck_after_terminal(self):
        nonlocal pump_task
        pump_task = asyncio.create_task(uncancellable_pump())
        self._interactive_pump = pump_task
        try:
            async for event in self._start.events:
                if event.type == EventType.EXITED:
                    await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_stream.wait()

    monkeypatch.setattr(command_run_module.RunEventStreamer, "stream_events", stuck_after_terminal)

    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t-uncancellable-pump"),
            CLIEvent(type=EventType.EXITED, task_id="t-uncancellable-pump", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
    )
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert task not in _ACTIVE_STREAM_TASKS
        assert pump_task is not None
        assert not pump_task.done()
    finally:
        release_pump.set()
        release_stream.set()
        if pump_task is not None and not pump_task.done():
            await pump_task
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_real_finally_tracks_uncancellable_interactive_pump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_FINALIZE_GRACE_SEC", 0.02, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CANCEL_GRACE_SEC", 0.01, raising=False)
    monkeypatch.setattr(run_event_streamer_module, "_INTERACTIVE_PUMP_CANCEL_GRACE_SEC", 0.005, raising=False)
    pump_task: asyncio.Task | None = None
    release_pump = asyncio.Event()

    async def uncancellable_pump(self):
        nonlocal pump_task
        pump_task = asyncio.current_task()
        while not release_pump.is_set():
            try:
                await release_pump.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(command_run_module.RunEventStreamer, "pump_structured_reply", uncancellable_pump)

    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t-real-pump"),
            CLIEvent(type=EventType.EXITED, task_id="t-real-pump", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
    )
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert pump_task is not None
        assert pump_task in run_event_streamer_module._ABANDONED_INTERACTIVE_PUMP_TASKS
    finally:
        release_pump.set()
        if pump_task is not None and not pump_task.done():
            await pump_task
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_schedules_queued_uploads_before_interactive_finalization_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_FINALIZE_GRACE_SEC", 0.01, raising=False)
    upload_scheduled = False

    def queued_upload_scheduler(message: DummyMessage, user_id: int, task_id: str) -> None:
        nonlocal upload_scheduled
        upload_scheduled = True

    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t-final-upload"),
            CLIEvent(type=EventType.EXITED, task_id="t-final-upload", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
    )
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        queued_upload_scheduler=queued_upload_scheduler,
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert task not in _ACTIVE_STREAM_TASKS
        assert upload_scheduled is True
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_schedules_queued_uploads_when_terminal_flush_is_canceled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_CHECK_INTERVAL_SEC", 0.005, raising=False)
    monkeypatch.setattr(command_run_module, "_STREAM_WATCHDOG_FINALIZE_GRACE_SEC", 0.01, raising=False)
    upload_scheduled = False

    async def stuck_flush(self) -> bool:
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(command_run_module.PresenterOutputDispatcher, "flush", stuck_flush)

    def queued_upload_scheduler(message: DummyMessage, user_id: int, task_id: str) -> None:
        nonlocal upload_scheduled
        upload_scheduled = True

    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t-flush"),
            CLIEvent(type=EventType.EXITED, task_id="t-flush", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        queued_upload_scheduler=queued_upload_scheduler,
    )

    assert task is not None
    try:
        await asyncio.sleep(0.1)
        assert task.done()
        assert task not in _ACTIVE_STREAM_TASKS
        assert upload_scheduled is True
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_prompt_and_stream_reports_started_output_and_success() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="hello\nworld\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )

    await _run_and_wait(message=message, task_service=task_service)

    assert message.answers[0] == "⏳ 处理中… [t1]"
    lifecycle = message.sent_messages[0]
    assert message.answers[1] == "hello\nworld"
    assert "✅ 完成 [t1]" in lifecycle.edits[-1]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_sends_started_message_when_lifecycle_edit_fails() -> None:
    message = DummyMessage(fail_first_edit=True)
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )

    await _run_and_wait(message=message, task_service=task_service)

    # When first edit fails, started message is sent as answer, but completion still edits lifecycle
    assert "⏳ 处理中… [t1]" in message.answers
    assert any("✅ 完成" in edit for msg in message.sent_messages for edit in msg.edits) or any("✅ 完成" in a for a in message.answers)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_updates_tool_message_to_success() -> None:
    running_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.RUNNING,
    )
    success_tool = ToolCallRecord(
        tool_use_id="tool-1",
        name="Bash",
        input={"command": "pytest -q"},
        status=ToolStatus.SUCCESS,
    )
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_sessions=[
            _structured_session(phase=SessionPhase.WAITING_FOR_INPUT),
            _structured_session(phase=SessionPhase.PROCESSING, tool_calls={"tool-1": running_tool}),
            _structured_session(phase=SessionPhase.PROCESSING, tool_calls={"tool-1": running_tool}),
            _structured_session(phase=SessionPhase.WAITING_FOR_INPUT, tool_calls={"tool-1": success_tool}),
            _structured_session(phase=SessionPhase.WAITING_FOR_INPUT, tool_calls={"tool-1": success_tool}),
        ],
        event_delays=[0.0, 0.16],
    )

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.25)

    tool_messages = [
        sent for sent in message.sent_messages if "工具: Bash" in sent.text or any("工具: Bash" in edit for edit in sent.edits)
    ]
    assert len(tool_messages) == 1
    assert any("执行中" in answer and "工具: Bash" in answer for answer in message.answers)
    assert "执行完成" in tool_messages[0].text or any("执行完成" in edit for edit in tool_messages[0].edits)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_aggregates_top_level_file_tools() -> None:
    grep_tool = ToolCallRecord(
        tool_use_id="grep-1",
        name="Grep",
        input={"pattern": "SessionStore"},
        status=ToolStatus.SUCCESS,
    )
    read_1_running = ToolCallRecord(
        tool_use_id="read-1",
        name="Read",
        input={"file_path": "app/services/session_store.py"},
        status=ToolStatus.RUNNING,
    )
    read_1_success = ToolCallRecord(
        tool_use_id="read-1",
        name="Read",
        input={"file_path": "app/services/session_store.py"},
        status=ToolStatus.SUCCESS,
    )
    read_2_success = ToolCallRecord(
        tool_use_id="read-2",
        name="Read",
        input={"file_path": "app/bot/handlers/command_run.py"},
        status=ToolStatus.SUCCESS,
    )
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_sessions=[
            _structured_session(phase=SessionPhase.WAITING_FOR_INPUT),
            _structured_session(phase=SessionPhase.PROCESSING, tool_calls={"grep-1": grep_tool, "read-1": read_1_running}),
            _structured_session(phase=SessionPhase.PROCESSING, tool_calls={"grep-1": grep_tool, "read-1": read_1_running}),
            _structured_session(
                phase=SessionPhase.WAITING_FOR_INPUT,
                tool_calls={"grep-1": grep_tool, "read-1": read_1_success, "read-2": read_2_success},
            ),
            _structured_session(
                phase=SessionPhase.WAITING_FOR_INPUT,
                tool_calls={"grep-1": grep_tool, "read-1": read_1_success, "read-2": read_2_success},
            ),
        ],
        event_delays=[0.0, 0.16],
    )

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.25)

    file_tool_messages = [
        sent for sent in message.sent_messages if "文件检索" in sent.text or any("文件检索" in edit for edit in sent.edits)
    ]
    assert len(file_tool_messages) == 1
    assert any("🔄 文件检索 · 执行中" in answer for answer in message.answers)
    assert "✅ 文件检索 · 完成" in file_tool_messages[0].text
    assert "读取 2 个文件" in file_tool_messages[0].text
    all_tool_messages = "\n".join(message.answers + [edit for sent in message.sent_messages for edit in sent.edits])
    assert "工具: Read" not in all_tool_messages
    assert "工具: Grep" not in all_tool_messages


@pytest.mark.asyncio
async def test_run_prompt_and_stream_updates_subagent_aggregate_message() -> None:
    agent_1_running = ToolCallRecord(
        tool_use_id="agent-1",
        name="Agent",
        input={"description": "项目架构扫描"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="read-1",
                name="Read",
                input={"file_path": "app/foo.py"},
                status=ToolStatus.RUNNING,
            )
        ],
    )
    agent_2_running = ToolCallRecord(
        tool_use_id="agent-2",
        name="Agent",
        input={"description": "测试质量扫描"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="glob-1",
                name="Glob",
                input={"path": "tests"},
                status=ToolStatus.RUNNING,
            )
        ],
    )
    agent_3_running = ToolCallRecord(
        tool_use_id="agent-3",
        name="Agent",
        input={"description": "安全性能扫描"},
        status=ToolStatus.RUNNING,
        subagent_tools=[],
    )
    agent_1_success = ToolCallRecord(
        tool_use_id="agent-1",
        name="Agent",
        input={"description": "项目架构扫描"},
        status=ToolStatus.SUCCESS,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="read-1",
                name="Read",
                input={"file_path": "app/foo.py"},
                status=ToolStatus.SUCCESS,
            )
        ],
    )
    agent_2_partial = ToolCallRecord(
        tool_use_id="agent-2",
        name="Agent",
        input={"description": "测试质量扫描"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="glob-1",
                name="Glob",
                input={"path": "tests"},
                status=ToolStatus.SUCCESS,
            ),
            SubagentToolCall(
                tool_use_id="grep-1",
                name="Grep",
                input={"pattern": "pytest"},
                status=ToolStatus.RUNNING,
            ),
        ],
    )
    agent_3_partial = ToolCallRecord(
        tool_use_id="agent-3",
        name="Agent",
        input={"description": "安全性能扫描"},
        status=ToolStatus.RUNNING,
        subagent_tools=[
            SubagentToolCall(
                tool_use_id="read-2",
                name="Read",
                input={"file_path": "app/bar.py"},
                status=ToolStatus.RUNNING,
            )
        ],
    )
    agent_2_success = ToolCallRecord(
        tool_use_id="agent-2",
        name="Agent",
        input={"description": "测试质量扫描"},
        status=ToolStatus.SUCCESS,
        subagent_tools=[],
    )
    agent_3_success = ToolCallRecord(
        tool_use_id="agent-3",
        name="Agent",
        input={"description": "安全性能扫描"},
        status=ToolStatus.SUCCESS,
        subagent_tools=[],
    )
    duplicate_glob = ToolCallRecord(
        tool_use_id="glob-1",
        name="Glob",
        input={"path": "tests"},
        status=ToolStatus.RUNNING,
    )
    duplicate_read = ToolCallRecord(
        tool_use_id="read-2",
        name="Read",
        input={"file_path": "app/bar.py"},
        status=ToolStatus.RUNNING,
    )
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_sessions=[
            _structured_session(phase=SessionPhase.WAITING_FOR_INPUT),
            _structured_session(
                phase=SessionPhase.PROCESSING,
                tool_calls={"agent-1": agent_1_running, "agent-2": agent_2_running, "agent-3": agent_3_running},
            ),
            _structured_session(
                phase=SessionPhase.PROCESSING,
                tool_calls={"agent-1": agent_1_running, "agent-2": agent_2_running, "agent-3": agent_3_running},
            ),
            _structured_session(
                phase=SessionPhase.PROCESSING,
                tool_calls={
                    "agent-1": agent_1_success,
                    "agent-2": agent_2_partial,
                    "agent-3": agent_3_partial,
                    "glob-1": duplicate_glob,
                    "read-2": duplicate_read,
                },
            ),
            _structured_session(
                phase=SessionPhase.PROCESSING,
                tool_calls={
                    "agent-1": agent_1_success,
                    "agent-2": agent_2_partial,
                    "agent-3": agent_3_partial,
                    "glob-1": duplicate_glob,
                    "read-2": duplicate_read,
                },
            ),
            _structured_session(
                phase=SessionPhase.WAITING_FOR_INPUT,
                tool_calls={"agent-1": agent_1_success, "agent-2": agent_2_success, "agent-3": agent_3_success},
            ),
            _structured_session(
                phase=SessionPhase.WAITING_FOR_INPUT,
                tool_calls={"agent-1": agent_1_success, "agent-2": agent_2_success, "agent-3": agent_3_success},
            ),
        ],
        event_delays=[0.0, 0.24],
    )

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.36)

    aggregate_messages = [sent for sent in message.sent_messages if "agents" in sent.text or any("agents" in edit for edit in sent.edits)]
    assert len(aggregate_messages) == 1
    aggregate_message = aggregate_messages[0]
    assert any("🔄 3 agents running" in answer for answer in message.answers)
    assert any("🔄 测试质量扫描 · 2 tool uses · Running" in edit for edit in aggregate_message.edits)
    assert "✅ 3 agents finished" in aggregate_message.text
    assert "项目架构扫描 · 1 tool uses · Done" in aggregate_message.text
    assert "测试质量扫描 · 2 tool uses · Done" in aggregate_message.text
    assert "安全性能扫描 · 1 tool uses · Done" in aggregate_message.text
    all_tool_messages = "\n".join(message.answers + [edit for sent in message.sent_messages for edit in sent.edits])
    assert "工具: Agent" not in all_tool_messages
    assert "工具: Glob" not in all_tool_messages
    assert "工具: Read" not in all_tool_messages


@pytest.mark.asyncio
async def test_run_prompt_and_stream_updates_claude_task_list_without_tool_spam() -> None:
    create_1 = ToolCallRecord(
        tool_use_id="create-1",
        name="TaskCreate",
        input={"subject": "梳理项目结构", "activeForm": "梳理项目结构"},
        status=ToolStatus.SUCCESS,
        structured_result={"task": {"id": "1", "subject": "梳理项目结构"}},
    )
    create_2 = ToolCallRecord(
        tool_use_id="create-2",
        name="TaskCreate",
        input={"subject": "评估当前改动", "activeForm": "评估当前改动"},
        status=ToolStatus.SUCCESS,
        structured_result={"task": {"id": "2", "subject": "评估当前改动"}},
    )
    create_3 = ToolCallRecord(
        tool_use_id="create-3",
        name="TaskCreate",
        input={"subject": "形成优化建议", "activeForm": "形成优化建议"},
        status=ToolStatus.SUCCESS,
        structured_result={"task": {"id": "3", "subject": "形成优化建议"}},
    )
    update_1_running = ToolCallRecord(
        tool_use_id="update-1-running",
        name="TaskUpdate",
        input={"taskId": "1", "status": "in_progress"},
        status=ToolStatus.SUCCESS,
    )
    update_1_completed = ToolCallRecord(
        tool_use_id="update-1-completed",
        name="TaskUpdate",
        input={"taskId": "1", "status": "completed"},
        status=ToolStatus.SUCCESS,
    )
    update_2_running = ToolCallRecord(
        tool_use_id="update-2-running",
        name="TaskUpdate",
        input={"taskId": "2", "status": "in_progress"},
        status=ToolStatus.SUCCESS,
    )
    update_2_completed = ToolCallRecord(
        tool_use_id="update-2-completed",
        name="TaskUpdate",
        input={"taskId": "2", "status": "completed"},
        status=ToolStatus.SUCCESS,
    )
    update_3_running = ToolCallRecord(
        tool_use_id="update-3-running",
        name="TaskUpdate",
        input={"taskId": "3", "status": "in_progress"},
        status=ToolStatus.SUCCESS,
    )
    update_3_completed = ToolCallRecord(
        tool_use_id="update-3-completed",
        name="TaskUpdate",
        input={"taskId": "3", "status": "completed"},
        status=ToolStatus.SUCCESS,
    )
    glob_tool = ToolCallRecord(
        tool_use_id="glob-1",
        name="Glob",
        input={"pattern": "**/*.py"},
        status=ToolStatus.RUNNING,
    )
    read_tool = ToolCallRecord(
        tool_use_id="read-1",
        name="Read",
        input={"file_path": "app/foo.py"},
        status=ToolStatus.RUNNING,
    )
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_sessions=[
            _structured_session(phase=SessionPhase.WAITING_FOR_INPUT),
            _structured_session(
                phase=SessionPhase.PROCESSING,
                tool_calls={
                    "create-1": create_1,
                    "create-2": create_2,
                    "create-3": create_3,
                    "update-1-running": update_1_running,
                    "glob-1": glob_tool,
                },
            ),
            _structured_session(
                phase=SessionPhase.PROCESSING,
                tool_calls={
                    "create-1": create_1,
                    "create-2": create_2,
                    "create-3": create_3,
                    "update-1-running": update_1_running,
                    "glob-1": glob_tool,
                },
            ),
            _structured_session(
                phase=SessionPhase.PROCESSING,
                tool_calls={
                    "create-1": create_1,
                    "create-2": create_2,
                    "create-3": create_3,
                    "update-1-running": update_1_running,
                    "update-1-completed": update_1_completed,
                    "update-2-running": update_2_running,
                    "glob-1": glob_tool,
                    "read-1": read_tool,
                },
            ),
            _structured_session(
                phase=SessionPhase.WAITING_FOR_INPUT,
                tool_calls={
                    "create-1": create_1,
                    "create-2": create_2,
                    "create-3": create_3,
                    "update-1-running": update_1_running,
                    "update-1-completed": update_1_completed,
                    "update-2-running": update_2_running,
                    "update-2-completed": update_2_completed,
                    "update-3-running": update_3_running,
                    "update-3-completed": update_3_completed,
                    "glob-1": glob_tool,
                    "read-1": read_tool,
                },
            ),
        ],
        event_delays=[0.0, 0.24],
    )

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.36)

    task_list_messages = [
        sent for sent in message.sent_messages if "任务列表" in sent.text or any("任务列表" in edit for edit in sent.edits)
    ]
    assert len(task_list_messages) == 1
    task_list_message = task_list_messages[0]
    assert any("=&gt; 🔄 1. 梳理项目结构 - 执行中" in answer for answer in message.answers)
    assert task_list_message.edits
    assert "当前: 无（全部完成）" in task_list_message.text
    assert "3. 形成优化建议 - 完成" in task_list_message.text
    all_tool_messages = "\n".join(message.answers + [edit for sent in message.sent_messages for edit in sent.edits])
    assert "工具: TaskCreate" not in all_tool_messages
    assert "工具: TaskUpdate" not in all_tool_messages
    assert "工具: Glob" not in all_tool_messages
    assert "工具: Read" not in all_tool_messages


@pytest.mark.asyncio
async def test_run_prompt_and_stream_strips_markers_and_marks_stderr() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="TGCLI_BEGIN\n正文\nTGCLI_DONE\n"),
            CLIEvent(type=EventType.STDERR, task_id="t1", content="boom\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )

    await _run_and_wait(message=message, task_service=task_service)

    assert "TGCLI_BEGIN" not in "\n".join(message.answers)
    assert "TGCLI_DONE" not in "\n".join(message.answers)
    assert "正文" in message.answers[1]
    assert "[stderr] boom" in message.answers[1]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_reports_failed_with_hint_and_truncation() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.FAILED, task_id="t1", error="tmux session lost"),
        ],
        _status(task_status=TaskStatus.FAILED, truncated=True),
    )

    await _run_and_wait(message=message, task_service=task_service)

    assert len(message.sent_messages) >= 1
    lifecycle = message.sent_messages[0]
    assert any("❌ 失败 [t1]" in edit for edit in lifecycle.edits)
    assert any("（输出已截断）" in edit for edit in lifecycle.edits)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_reports_timeout() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.TIMEOUT, task_id="t1", error="deadline exceeded"),
        ],
        _status(task_status=TaskStatus.TIMEOUT),
    )

    await _run_and_wait(message=message, task_service=task_service)

    lifecycle = message.sent_messages[0]
    assert any("⏰ 超时 [t1]" in edit for edit in lifecycle.edits)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_reports_canceled() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.CANCELED, task_id="t1", error="user canceled"),
        ],
        _status(task_status=TaskStatus.CANCELED),
    )

    await _run_and_wait(message=message, task_service=task_service)

    lifecycle = message.sent_messages[0]
    assert any("🚫 已取消 [t1]" in edit for edit in lifecycle.edits)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_prefers_structured_reply_in_interactive_mode() -> None:
    message = DummyMessage()
    turns: list[ConversationTurn] = []
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="噪音\nTGCLI_BEGIN\n正文\nTGCLI_DONE\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.03, 0.1],
    )

    async def append_new_turn() -> None:
        await asyncio.sleep(0.02)
        turns.append(ConversationTurn(turn_id="turn-1", role="assistant", text="\n干净正文\n", is_complete=True))

    updater = asyncio.create_task(append_new_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.2)
    await updater

    assert "噪音" not in "\n".join(message.answers)
    assert message.answers[1] == "干净正文"
    assert task_service._structured_reply_turn_id == "turn-1"


@pytest.mark.asyncio
async def test_run_prompt_and_stream_does_not_ack_structured_reply_when_send_fails() -> None:
    message = DummyMessage(fail_on_texts={"干净正文"})
    turns: list[ConversationTurn] = []
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="噪音\nTGCLI_BEGIN\n正文\nTGCLI_DONE\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.03, 0.1],
    )

    async def append_new_turn() -> None:
        await asyncio.sleep(0.02)
        turns.append(ConversationTurn(turn_id="turn-1", role="assistant", text="\n干净正文\n", is_complete=True))

    updater = asyncio.create_task(append_new_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.2)
    await updater

    assert "干净正文" not in "\n".join(message.answers)
    assert task_service._structured_reply_turn_id is None


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_ignores_old_turn_and_emits_new_completed_turn() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-old", role="assistant", text="\n旧回复\n", is_complete=True)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="tmux 噪音\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.03, 0.12],
    )

    async def append_new_turn() -> None:
        await asyncio.sleep(0.02)
        turns.append(ConversationTurn(turn_id="turn-new", role="assistant", text="\n新回复\n", is_complete=True))

    updater = asyncio.create_task(append_new_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.25)
    await updater

    assert "旧回复" not in "\n".join(message.answers)
    assert "tmux 噪音" not in "\n".join(message.answers)
    assert message.answers[1] == "新回复"


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_does_not_emit_incomplete_turn() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n半截回复\n", is_complete=False)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="tmux 噪音\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.03, 0.08],
    )

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.12)

    assert all("半截回复" not in item for item in message.answers)
    assert all("tmux 噪音" not in item for item in message.answers)
    assert "结构化回复暂不可用，已回退为原始输出。" in message.answers
    # Success is now edited into the lifecycle message, not a separate answer
    lifecycle = message.sent_messages[0]
    assert any("✅ 完成" in edit for edit in lifecycle.edits)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_falls_back_to_stdout_without_structured_session() -> None:
    message = DummyMessage()
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="原始输出\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        event_delays=[0.0, 0.03, 0.08],
    )
    task_service.get_structured_session = AsyncMock(return_value=None)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.12)

    # Interactive mode always suppresses raw STDOUT to prevent duplicates
    # with the structured reply system. Only lifecycle messages are sent.
    assert "原始输出" not in "\n".join(message.answers)
    lifecycle = message.sent_messages[0]
    assert any("✅ 完成" in edit for edit in lifecycle.edits)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_continues_after_message_send_failure() -> None:
    message = DummyMessage(fail_on_calls={1})
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="hello\nworld\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
    )

    await _run_and_wait(message=message, task_service=task_service)

    # lifecycle_message send failed (call 1), so ⏳ never appears in answers.
    # Output and completion still get through.
    assert "hello\nworld" in message.answers
    assert any("✅ 完成" in a for a in message.answers) or any("✅ 完成" in edit for msg in message.sent_messages for edit in msg.edits)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_emits_late_structured_turn_before_exit() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-old", role="assistant", text="\n旧回复\n", is_complete=True)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="tmux 噪音\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.02, 0.06],
    )

    async def append_new_turn() -> None:
        await asyncio.sleep(0.05)
        turns.append(ConversationTurn(turn_id="turn-late", role="assistant", text="\n迟到回复\n", is_complete=True))

    updater = asyncio.create_task(append_new_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.18)
    await updater

    assert "旧回复" not in "\n".join(message.answers)
    assert "tmux 噪音" not in "\n".join(message.answers)
    assert "迟到回复" in message.answers[1]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_emits_turn_arriving_after_exit_event() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-old", role="assistant", text="\n旧回复\n", is_complete=True)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.02],
    )

    async def append_new_turn() -> None:
        await asyncio.sleep(0.06)
        turns.append(ConversationTurn(turn_id="turn-after-exit", role="assistant", text="\n退出后补到的回复\n", is_complete=True))

    updater = asyncio.create_task(append_new_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.2)
    await updater

    assert "旧回复" not in "\n".join(message.answers)
    assert "退出后补到的回复" in "\n".join(message.answers)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_uses_task_bound_session_after_context_drift() -> None:
    message = DummyMessage()
    task_turns = [ConversationTurn(turn_id="turn-old", role="assistant", text="\n旧回复\n", is_complete=True)]
    task_session = SimpleNamespace(
        session_id="claude-session-task",
        phase=SessionPhase.WAITING_FOR_INPUT,
        turns=task_turns,
        pending_permission=None,
        tool_calls={},
    )
    drift_session = SimpleNamespace(
        session_id="claude-session-other",
        phase=SessionPhase.WAITING_FOR_APPROVAL,
        turns=[],
        pending_permission=None,
        tool_calls={},
    )
    current_session = {"value": task_session}
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        event_delays=[0.0, 0.08],
    )

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        return current_session["value"]

    async def get_structured_session_for_task(*, task_id: str, user_id: int, log_missing: bool = True):
        return task_session

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)
    task_service.get_structured_session_for_task = AsyncMock(side_effect=get_structured_session_for_task)

    async def drift_context_and_append_reply() -> None:
        await asyncio.sleep(0.02)
        current_session["value"] = drift_session
        task_turns.append(ConversationTurn(turn_id="turn-task-new", role="assistant", text="\n任务对应回复\n", is_complete=True))

    updater = asyncio.create_task(drift_context_and_append_reply())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.18)
    await updater

    answers = "\n".join(message.answers)
    assert "旧回复" not in answers
    assert "任务对应回复" in answers
    assert "结构化回复暂不可用，已回退为原始输出。" not in message.answers


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_reports_pending_permission_once() -> None:
    message = DummyMessage()
    permission_gateway = FakePermissionGateway()
    pending = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n已完成回复\n", is_complete=True)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        return SimpleNamespace(
            session_id="claude-session-1",
            phase=SessionPhase.WAITING_FOR_APPROVAL,
            turns=turns,
            pending_permission=pending,
        )

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.14, permission_gateway=permission_gateway)

    expected_prompt = permission_gateway.message_builder.build_permission_prompt(
        PermissionPromptInput(
            tool_name="Bash",
            tool_input={"command": "pwd"},
            cwd="",
            session_id="claude-session-1",
            session_title=None,
        )
    )
    expected_rendered = render_markdownish_to_telegram_html(expected_prompt)
    assert message.answers.count(expected_rendered) == 1
    permission_index = message.answers.index(expected_rendered)
    reply_markup = message.reply_markups[permission_index]
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    assert [button.text for button in reply_markup.inline_keyboard[0]] == ["✅ Approve", "❌ Deny"]
    assert [button.callback_data for button in reply_markup.inline_keyboard[0]] == ["perm:tok12345:allow", "perm:tok12345:deny"]
    assert permission_gateway.auto_approve_calls == [("claude-session-1", SessionOrigin.OWNED, 1, "tool-1")]
    assert permission_gateway.registrations == [("tool-1", "claude-session-1", SessionOrigin.OWNED, 1)]
    assert task_service._structured_permission_key == "tool-1:Bash"


@pytest.mark.asyncio
async def test_run_prompt_and_stream_does_not_ack_permission_when_prompt_send_fails() -> None:
    message = DummyMessage(fail_on_texts={"请求权限"})
    permission_gateway = FakePermissionGateway()
    pending = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n已完成回复\n", is_complete=True)]
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        return SimpleNamespace(
            session_id="claude-session-1",
            phase=SessionPhase.WAITING_FOR_APPROVAL,
            turns=turns,
            pending_permission=pending,
        )

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.14, permission_gateway=permission_gateway)

    expected_prompt = permission_gateway.message_builder.build_permission_prompt(
        PermissionPromptInput(
            tool_name="Bash",
            tool_input={"command": "pwd"},
            cwd="",
            session_id="claude-session-1",
            session_title=None,
        )
    )
    assert render_markdownish_to_telegram_html(expected_prompt) not in message.answers
    assert task_service._structured_permission_key is None


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_reports_user_question_once() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n已收到问题\n", is_complete=True)]
    empty_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_tool = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理方式",
                    "question": "这两条误写到项目级的记忆，你要我怎么处理？",
                    "options": [
                        {"label": "迁到全局(推荐)", "description": "保留记忆内容并迁移"},
                        {"label": "直接删除", "description": "删除项目级这两条记忆"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    question_session.tool_calls = {"tool-ask-1": question_tool}
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    responses = iter([empty_session, empty_session, question_session])

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        try:
            return next(responses)
        except StopIteration:
            return question_session

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.14)

    expected_prompt = build_user_question_prompt(
        SimpleNamespace(
            tool_use_id="tool-ask-1",
            question_index=0,
            total_questions=1,
            header="处理方式",
            question="这两条误写到项目级的记忆，你要我怎么处理？",
            options=(
                SimpleNamespace(label="迁到全局(推荐)", description="保留记忆内容并迁移"),
                SimpleNamespace(label="直接删除", description="删除项目级这两条记忆"),
            ),
            multi_select=False,
        ),
        session_id="claude-session-1",
    )
    assert message.answers.count(expected_prompt) == 1
    question_index = message.answers.index(expected_prompt)
    reply_markup = message.reply_markups[question_index]
    assert reply_markup is not None
    assert [button.text for row in reply_markup.inline_keyboard for button in row] == ["迁到全局(推荐)", "直接删除"]
    assert [button.callback_data for row in reply_markup.inline_keyboard for button in row] == [
        "ask:tool-ask-1:0:0",
        "ask:tool-ask-1:0:1",
    ]
    assert task_service._structured_user_question_key == "tool-ask-1:0"


@pytest.mark.asyncio
async def test_run_prompt_and_stream_does_not_ack_user_question_when_prompt_send_fails() -> None:
    message = DummyMessage(fail_on_texts={"这两条误写到项目级的记忆"})
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n已收到问题\n", is_complete=True)]
    empty_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_tool = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理方式",
                    "question": "这两条误写到项目级的记忆，你要我怎么处理？",
                    "options": [
                        {"label": "迁到全局(推荐)", "description": "保留记忆内容并迁移"},
                        {"label": "直接删除", "description": "删除项目级这两条记忆"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    question_session.tool_calls = {"tool-ask-1": question_tool}
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    responses = iter([empty_session, empty_session, question_session])

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        try:
            return next(responses)
        except StopIteration:
            return question_session

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.14)

    assert all("这两条误写到项目级的记忆" not in answer for answer in message.answers)
    assert task_service._structured_user_question_key is None


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_reports_multi_select_user_question_with_submit_button() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n已收到问题\n", is_complete=True)]
    empty_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_tool = ToolCallRecord(
        tool_use_id="tool-ask-multi",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理方式",
                    "question": "这次要保留哪些动作？",
                    "options": [
                        {"label": "保留日志", "description": "继续输出调试日志"},
                        {"label": "保留测试", "description": "继续保留回归测试"},
                    ],
                    "multiSelect": True,
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    question_session.tool_calls = {"tool-ask-multi": question_tool}
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    responses = iter([empty_session, empty_session, question_session])

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        try:
            return next(responses)
        except StopIteration:
            return question_session

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.14)

    question_index = message.answers.index(
        build_user_question_prompt(
            SimpleNamespace(
                tool_use_id="tool-ask-multi",
                question_index=0,
                total_questions=1,
                header="处理方式",
                question="这次要保留哪些动作？",
                options=(
                    SimpleNamespace(label="保留日志", description="继续输出调试日志"),
                    SimpleNamespace(label="保留测试", description="继续保留回归测试"),
                ),
                multi_select=True,
            ),
            session_id="claude-session-1",
        )
    )
    reply_markup = message.reply_markups[question_index]
    assert reply_markup is not None
    assert [button.text for row in reply_markup.inline_keyboard for button in row] == [
        "☐ 保留日志",
        "☐ 保留测试",
        "提交选择",
    ]
    assert [button.callback_data for row in reply_markup.inline_keyboard for button in row] == [
        "ask:toggle:tool-ask-multi:0:0",
        "ask:toggle:tool-ask-multi:0:1",
        "ask:submit:tool-ask-multi:0",
    ]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_reports_only_first_question_for_multi_question_prompt() -> None:
    message = DummyMessage()
    turns = [ConversationTurn(turn_id="turn-1", role="assistant", text="\n已收到问题\n", is_complete=True)]
    empty_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls={},
    )
    question_tool = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理范围",
                    "question": "你说的范围我理解为这三块之一，具体按哪种处理？",
                    "options": [
                        {"label": "当前相关改动(推荐)", "description": "只处理相关已改动文件"},
                        {"label": "三个目录全部", "description": "范围非常大"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "提交前置",
                    "question": "按你的 CLAUDE.md，要修改代码前先提交现有改动。现在是否允许我先做这一步？",
                    "options": [
                        {"label": "允许先提交(推荐)", "description": "先提交后继续"},
                        {"label": "暂不允许", "description": "先不改代码"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
        status=ToolStatus.RUNNING,
    )
    question_session.tool_calls = {"tool-ask-1": question_tool}
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    responses = iter([empty_session, empty_session, question_session])

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        try:
            return next(responses)
        except StopIteration:
            return question_session

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.14)

    first_prompt = build_user_question_prompt(
        SimpleNamespace(
            tool_use_id="tool-ask-1",
            question_index=0,
            total_questions=2,
            header="处理范围",
            question="你说的范围我理解为这三块之一，具体按哪种处理？",
            options=(
                SimpleNamespace(label="当前相关改动(推荐)", description="只处理相关已改动文件"),
                SimpleNamespace(label="三个目录全部", description="范围非常大"),
            ),
            multi_select=False,
        ),
        session_id="claude-session-1",
    )
    second_prompt = build_user_question_prompt(
        SimpleNamespace(
            tool_use_id="tool-ask-1",
            question_index=1,
            total_questions=2,
            header="提交前置",
            question="按你的 CLAUDE.md，要修改代码前先提交现有改动。现在是否允许我先做这一步？",
            options=(
                SimpleNamespace(label="允许先提交(推荐)", description="先提交后继续"),
                SimpleNamespace(label="暂不允许", description="先不改代码"),
            ),
            multi_select=False,
        ),
        session_id="claude-session-1",
    )

    assert message.answers.count(first_prompt) == 1
    assert second_prompt not in message.answers
    question_index = message.answers.index(first_prompt)
    reply_markup = message.reply_markups[question_index]
    assert reply_markup is not None
    assert [button.text for row in reply_markup.inline_keyboard for button in row] == [
        "当前相关改动(推荐)",
        "三个目录全部",
    ]
    assert [button.callback_data for row in reply_markup.inline_keyboard for button in row] == [
        "ask:tool-ask-1:0:0",
        "ask:tool-ask-1:0:1",
    ]


@pytest.mark.asyncio
async def test_run_prompt_and_stream_interactive_emits_progress_update_immediately() -> None:
    message = DummyMessage()
    turns: list[ConversationTurn] = []
    tool_calls: dict[str, ToolCallRecord] = {}
    current_session = SimpleNamespace(
        session_id="claude-session-1",
        phase=SessionPhase.PROCESSING,
        turns=turns,
        pending_permission=None,
        tool_calls=tool_calls,
    )
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        event_delays=[0.0, 0.12],
    )

    async def get_structured_session(user_id: int, *, log_missing: bool = True):
        return current_session

    task_service.get_structured_session = AsyncMock(side_effect=get_structured_session)

    async def publish_progress() -> None:
        await asyncio.sleep(0.02)
        tool_calls["tool-1"] = ToolCallRecord(
            tool_use_id="tool-1",
            name="Bash",
            input={"command": "pytest -q"},
            status=ToolStatus.RUNNING,
        )
        await asyncio.sleep(0.04)
        turns.append(ConversationTurn(turn_id="turn-1", role="assistant", text="\n测试完成\n", is_complete=True))
        current_session.phase = SessionPhase.WAITING_FOR_INPUT

    updater = asyncio.create_task(publish_progress())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.24)
    await updater

    progress_message = build_tool_progress_message(tool_name="Bash", tool_input={"command": "pytest -q"})
    assert message.answers.count(progress_message) == 1
    progress_index = message.answers.index(progress_message)
    assert progress_index == 1
    assert message.reply_markups[progress_index] is None
    assert "测试完成" in message.answers


def test_render_markdownish_to_telegram_html_supports_bold_and_code_block() -> None:
    rendered = render_markdownish_to_telegram_html("**标题**\n\n```python\nprint('hi')\n```")

    assert rendered == "<b>标题</b>\n\n<pre><code>print(&#x27;hi&#x27;)</code></pre>"


@pytest.mark.asyncio
async def test_run_prompt_and_stream_renders_structured_reply_as_html() -> None:
    message = DummyMessage()
    turns: list[ConversationTurn] = []
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    async def append_markdown_turn() -> None:
        await asyncio.sleep(0.02)
        turns.append(
            ConversationTurn(
                turn_id="turn-md",
                role="assistant",
                text="**你好**\n\n```python\nprint('hi')\n```",
                is_complete=True,
            )
        )

    updater = asyncio.create_task(append_markdown_turn())
    await _run_and_wait(message=message, task_service=task_service, wait_sec=0.14)
    await updater

    assert "<b>你好</b>" in message.answers[1]
    assert "<pre><code>print(&#x27;hi&#x27;)</code></pre>" in message.answers[1]
    assert message.parse_modes[1] == ParseMode.HTML


@pytest.mark.asyncio
async def test_run_prompt_and_stream_splits_long_code_block_reply_into_valid_html_chunks() -> None:
    message = DummyMessage()
    turns: list[ConversationTurn] = []
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    async def append_markdown_turn() -> None:
        await asyncio.sleep(0.02)
        turns.append(
            ConversationTurn(
                turn_id="turn-md-long",
                role="assistant",
                text="```python\n1234567890\nabcdefghij\n```",
                is_complete=True,
            )
        )

    updater = asyncio.create_task(append_markdown_turn())
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=24, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )
    await asyncio.sleep(0.14)
    if task is not None:
        await task
    await updater

    reply_chunks = [item for item in message.answers if item.startswith("<pre><code>")]
    assert reply_chunks == [
        "<pre><code>1234567890</code></pre>",
        "<pre><code>abcdefghij</code></pre>",
    ]
    reply_indexes = [message.answers.index(chunk) for chunk in reply_chunks]
    assert all(message.parse_modes[index] == ParseMode.HTML for index in reply_indexes)


@pytest.mark.asyncio
async def test_run_prompt_and_stream_does_not_truncate_long_structured_reply_preview() -> None:
    message = DummyMessage()
    long_reply = "A" * 1905
    turns: list[ConversationTurn] = []
    task_service = DummyTaskService(
        [
            CLIEvent(type=EventType.STARTED, task_id="t1", content="tmux_session=tgcli_user_1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        _status(task_status=TaskStatus.SUCCEEDED),
        interactive=True,
        structured_turns=turns,
        event_delays=[0.0, 0.08],
    )

    async def append_long_turn() -> None:
        await asyncio.sleep(0.02)
        turns.append(
            ConversationTurn(
                turn_id="turn-long",
                role="assistant",
                text=long_reply,
                is_complete=True,
            )
        )

    updater = asyncio.create_task(append_long_turn())
    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=4096, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
    )
    await asyncio.sleep(0.14)
    if task is not None:
        await task
    await updater

    assert message.answers[1] == long_reply
    assert "输出片段过长" not in message.answers[1]
