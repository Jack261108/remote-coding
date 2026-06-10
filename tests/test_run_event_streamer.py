from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.bot.handlers.run_event_streamer import RunEventStreamer
from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus


class DummyTaskService:
    def __init__(self, status: TaskRecord) -> None:
        self._status = status

    async def get_status(self, task_id: str, user_id: int) -> TaskRecord:
        return self._status


class DummyDispatcher:
    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.flushed = 0

    async def push_text(self, text: str) -> bool:
        self.pushed.append(text)
        return True

    async def flush(self) -> bool:
        self.flushed += 1
        return True

    async def emit_presenter_messages(self, **kwargs) -> None:
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


def _streamer(events: list[CLIEvent]) -> tuple[RunEventStreamer, DummyDispatcher]:
    task = SimpleNamespace(task_id="t1", provider="claude_code", session_id="s1", workdir="/tmp")
    start = SimpleNamespace(task=task, events=_events(events), interactive=False)
    dispatcher = DummyDispatcher()
    streamer = RunEventStreamer(
        start=start,
        task_service=DummyTaskService(_status()),
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
    assert dispatcher.pushed == [f"stdout {secret}\n", f"[stderr] stderr {secret}\n"]


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
