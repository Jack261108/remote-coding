from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.bot.handlers import run_event_streamer as run_event_streamer_module
from app.bot.handlers.run_display_models import (
    DisplayEvent,
    DisplayEventKind,
    StreamTextDisplayPayload,
    TaskFailedDisplayPayload,
    TaskSucceededDisplayPayload,
)
from app.bot.handlers.run_event_streamer import RunEventStreamer
from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus


class DummyTaskService:
    def __init__(self, status: TaskRecord) -> None:
        self._status = status

    async def get_status(self, task_id: str, user_id: int) -> TaskRecord:
        return self._status


class DummyDispatcher:
    def __init__(self) -> None:
        self.display_events: list[tuple[DisplayEvent, object | None]] = []
        self.flushed = 0

    async def execute_display_event(self, event: DisplayEvent, *, lifecycle_message: object | None = None) -> None:
        self.display_events.append((event, lifecycle_message))

    async def flush(self) -> bool:
        self.flushed += 1
        return True

    async def emit_presenter_messages(self, **kwargs) -> None:
        return None

    async def emit_structured_reply(self, reply) -> None:
        return None


class DummyMessenger:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.answers: list[str] = []
        self.reactions: list[object] = []

    async def edit_message_safely(self, message, text: str) -> bool:
        self.edits.append(text)
        return True

    async def answer_safely(self, text: str, **kwargs) -> bool:
        self.answers.append(text)
        return True

    async def set_reaction(self, reaction) -> None:
        self.reactions.append(reaction)


class DummyPresenter:
    has_emitted_structured_reply = False
    has_announced_fallback = False

    def limit_reply_cursor(self, *, max_reply_started_at: datetime) -> None:
        return None

    async def wait_for_initial_update(self, *, timeout_sec: float) -> bool:
        return False

    def freeze_reply_cursor(self) -> None:
        return None


async def _events(items: list[CLIEvent]):
    for item in items:
        yield item


def _status() -> TaskRecord:
    return TaskRecord(
        task_id="t1",
        session_id="s1",
        user_id=42,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        timeout_sec=60,
        status=TaskStatus.SUCCEEDED,
        output_chars=0,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _streamer(
    events: list[CLIEvent],
    *,
    interactive: bool = False,
    status: TaskRecord | None = None,
) -> tuple[RunEventStreamer, DummyDispatcher]:
    task = SimpleNamespace(task_id="t1", provider="claude_code", session_id="s1", workdir="/tmp")
    start = SimpleNamespace(task=task, events=_events(events), interactive=interactive)
    dispatcher = DummyDispatcher()
    streamer = RunEventStreamer(
        start=start,
        task_service=DummyTaskService(status or _status()),
        user_id=42,
        presenter=DummyPresenter(),
        dispatcher=dispatcher,
        messenger=DummyMessenger(),
        lifecycle_message=None,
    )
    return streamer, dispatcher


@pytest.mark.asyncio
async def test_stream_events_does_not_log_raw_stdout_or_stderr_at_info(caplog) -> None:
    secret = "SECRET_TOKEN_DO_NOT_LOG"
    streamer, dispatcher = _streamer(
        [
            CLIEvent(type=EventType.STDOUT, task_id="t1", content=f"stdout {secret}\n"),
            CLIEvent(type=EventType.STDERR, task_id="t1", content=f"stderr {secret}\n"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ]
    )
    caplog.set_level(logging.INFO, logger="app.bot.handlers.run_event_streamer")

    await streamer.stream_events()

    assert secret not in caplog.text
    assert [event.kind for event, _ in dispatcher.display_events] == [
        DisplayEventKind.STREAM_TEXT,
        DisplayEventKind.STREAM_TEXT,
        DisplayEventKind.TASK_SUCCEEDED,
    ]
    stdout_payload = dispatcher.display_events[0][0].payload
    stderr_payload = dispatcher.display_events[1][0].payload
    success_payload = dispatcher.display_events[2][0].payload
    assert stdout_payload == StreamTextDisplayPayload(text=f"stdout {secret}\n")
    assert stderr_payload == StreamTextDisplayPayload(text=f"stderr {secret}\n", is_stderr=True)
    assert success_payload == TaskSucceededDisplayPayload(task_id="t1", duration="-", truncated=False, exit_code=0)
    assert dispatcher.flushed == 0


@pytest.mark.asyncio
async def test_interactive_stream_skips_raw_stdout_and_stderr() -> None:
    streamer, dispatcher = _streamer(
        [
            CLIEvent(type=EventType.STDOUT, task_id="t1", content="stdout"),
            CLIEvent(type=EventType.STDERR, task_id="t1", content="stderr"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        interactive=True,
    )

    await streamer.stream_events()

    assert [event.kind for event, _ in dispatcher.display_events] == [DisplayEventKind.TASK_SUCCEEDED]


@pytest.mark.parametrize("event_type", [EventType.FAILED, EventType.TIMEOUT, EventType.CANCELED])
@pytest.mark.asyncio
async def test_terminal_errors_emit_normalized_task_failed_display_event(event_type: EventType) -> None:
    status = _status()
    status.output_truncated = True
    streamer, dispatcher = _streamer(
        [
            CLIEvent(
                type=event_type,
                task_id="t1",
                error="TGCLI_BEGIN\r\n\x1b[31mboom\x1b[0m  \r\nTGCLI_DONE",
            )
        ],
        status=status,
    )

    await streamer.stream_events()

    assert len(dispatcher.display_events) == 1
    event, lifecycle_message = dispatcher.display_events[0]
    assert event.kind == DisplayEventKind.TASK_FAILED
    assert event.payload == TaskFailedDisplayPayload(
        event_type=event_type,
        task_id="t1",
        error_text="boom",
        duration="-",
        truncated=True,
    )
    assert lifecycle_message is None
    assert dispatcher.flushed == 0


@pytest.mark.asyncio
async def test_diff_snapshot_capture_does_not_block_event_loop() -> None:
    class BlockingDiffGenerator:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def capture_snapshot(self, workdir: str, gitignore_patterns: list[str]):
            self.started.set()
            self.release.wait(timeout=0.35)
            return {}

        def detect_modified_files(self, *, workdir: str, pre_snapshot, gitignore_patterns: list[str]):
            return []

        def generate_unified_diff(self, modified_files, pre_snapshot):
            return None

    diff_generator = BlockingDiffGenerator()
    task = SimpleNamespace(task_id="t1", provider="claude_code", session_id="s1", workdir="/tmp")
    start = SimpleNamespace(
        task=task,
        events=_events([CLIEvent(type=EventType.STARTED, task_id="t1"), CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0)]),
        interactive=False,
    )
    streamer = RunEventStreamer(
        start=start,
        task_service=DummyTaskService(_status()),
        user_id=42,
        presenter=DummyPresenter(),
        dispatcher=DummyDispatcher(),
        messenger=DummyMessenger(),
        lifecycle_message=None,
        diff_generator=diff_generator,
    )

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    stream_task = asyncio.create_task(streamer.stream_events())

    assert await asyncio.wait_for(asyncio.to_thread(diff_generator.started.wait, 1), timeout=1)
    elapsed = loop.time() - started_at
    diff_generator.release.set()
    await stream_task

    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_terminal_event_skips_diff_when_snapshot_capture_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    class BlockingDiffGenerator:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.detect_called = False

        def capture_snapshot(self, workdir: str, gitignore_patterns: list[str]):
            self.started.set()
            self.release.wait(timeout=1)
            return {}

        def detect_modified_files(self, *, workdir: str, pre_snapshot, gitignore_patterns: list[str]):
            self.detect_called = True
            return []

        def generate_unified_diff(self, modified_files, pre_snapshot):
            return None

    monkeypatch.setattr(run_event_streamer_module, "_SNAPSHOT_CAPTURE_TIMEOUT_SEC", 0.01)
    diff_generator = BlockingDiffGenerator()
    task = SimpleNamespace(task_id="t1", provider="claude_code", session_id="s1", workdir="/tmp")
    start = SimpleNamespace(
        task=task,
        events=_events([CLIEvent(type=EventType.STARTED, task_id="t1"), CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0)]),
        interactive=False,
    )
    streamer = RunEventStreamer(
        start=start,
        task_service=DummyTaskService(_status()),
        user_id=42,
        presenter=DummyPresenter(),
        dispatcher=DummyDispatcher(),
        messenger=DummyMessenger(),
        lifecycle_message=None,
        diff_generator=diff_generator,
    )

    stream_task = asyncio.create_task(streamer.stream_events())

    assert await asyncio.wait_for(asyncio.to_thread(diff_generator.started.wait, 1), timeout=1)
    await asyncio.wait_for(stream_task, timeout=0.5)
    diff_generator.release.set()

    assert diff_generator.detect_called is False


@pytest.mark.asyncio
async def test_consume_task_result_logs_failed_task_traceback(caplog) -> None:
    marker = "streamer background boom"

    async def fail() -> None:
        raise RuntimeError(marker)

    task = asyncio.create_task(fail(), name="streamer-pump")
    done, _ = await asyncio.wait({task}, timeout=1)
    assert task in done

    caplog.set_level(logging.WARNING, logger="app.bot.handlers.run_event_streamer")
    RunEventStreamer._consume_task_result(task)

    records = [record for record in caplog.records if record.message == "background task raised an exception"]
    assert records
    assert records[0].exc_info is not None
    assert records[0].exc_info[0] is RuntimeError
    assert marker in caplog.text
    assert "Traceback" in caplog.text
