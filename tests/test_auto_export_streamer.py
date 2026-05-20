"""Tests for auto-export integration in RunEventStreamer."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.handlers.command_run import run_prompt_and_stream
from app.bot.presenters.chunk_sender import ChunkSender
from app.domain.file_models import ExportResult
from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus


class DummyTaskService:
    def __init__(self, events: list[CLIEvent], status: TaskRecord | None = None) -> None:
        self._events = events
        self._status = status
        self._revision = 0

    async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
        task = SimpleNamespace(task_id="t1", provider="claude_code", session_id="s1", started_at=None, created_at=None)
        return SimpleNamespace(task=task, events=self._stream(), interactive=False)

    async def get_status(self, task_id: str, user_id: int):
        return self._status

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        return None

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True):
        return None

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return self._revision

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        return None, None

    async def acknowledge_structured_reply(self, user_id: int, **kwargs) -> None:
        pass

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None):
        return None

    async def acknowledge_structured_user_question(self, user_id: int, **kwargs) -> None:
        pass

    async def wait_for_structured_session_update(
        self, *, user_id: int, since_cursor: int, timeout_sec: float, task_id: str | None = None
    ) -> bool:
        await asyncio.sleep(timeout_sec)
        return True

    async def _stream(self):
        for event in self._events:
            yield event


class DummyMessage:
    """Minimal aiogram Message fake for testing."""

    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.chat = SimpleNamespace(id=100)
        self._answers: list[str] = []
        self._documents: list[object] = []

    async def answer(self, text: str, **kwargs) -> "DummyMessage":
        self._answers.append(text)
        new_msg = DummyMessage()
        new_msg.message_id = len(self._answers)
        return new_msg

    async def answer_document(self, document, **kwargs) -> "DummyMessage":
        self._documents.append(document)
        new_msg = DummyMessage()
        new_msg.message_id = 999
        return new_msg

    async def edit_text(self, text: str, **kwargs) -> None:
        self._answers.append(text)


def _make_record(output_chars: int) -> TaskRecord:
    return TaskRecord(
        task_id="t1",
        session_id="s1",
        user_id=42,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        timeout_sec=60,
        status=TaskStatus.SUCCEEDED,
        output_chars=output_chars,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_auto_export_triggers_when_output_exceeds_threshold(tmp_path: Path) -> None:
    """When output_chars exceeds threshold, export_markdown is called and document is sent."""
    record = _make_record(output_chars=5000)
    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
    ]
    task_service = DummyTaskService(events=events, status=record)

    # Create a temp markdown file to simulate export
    md_file = tmp_path / "task_t1.md"
    md_file.write_text("# Export content", encoding="utf-8")
    export_result = ExportResult(file_path=md_file, filename="task_t1.md", mime_type="text/markdown")

    result_exporter = MagicMock()
    result_exporter.should_auto_export.return_value = True
    result_exporter.export_markdown = AsyncMock(return_value=export_result)

    message = DummyMessage()

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=42,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        result_exporter=result_exporter,
    )
    if task is not None:
        await task

    result_exporter.should_auto_export.assert_called_once_with(5000)
    result_exporter.export_markdown.assert_awaited_once_with(record)
    assert len(message._documents) == 1


@pytest.mark.asyncio
async def test_auto_export_does_not_trigger_when_output_below_threshold() -> None:
    """When output_chars is below threshold, no export happens."""
    record = _make_record(output_chars=100)
    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
    ]
    task_service = DummyTaskService(events=events, status=record)

    result_exporter = MagicMock()
    result_exporter.should_auto_export.return_value = False
    result_exporter.export_markdown = AsyncMock()

    message = DummyMessage()

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=42,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        result_exporter=result_exporter,
    )
    if task is not None:
        await task

    result_exporter.should_auto_export.assert_called_once_with(100)
    result_exporter.export_markdown.assert_not_awaited()
    assert len(message._documents) == 0


@pytest.mark.asyncio
async def test_auto_export_does_not_trigger_on_failure() -> None:
    """Auto-export should not trigger when task fails."""
    record = _make_record(output_chars=5000)
    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.FAILED, task_id="t1", error="something broke"),
    ]
    task_service = DummyTaskService(events=events, status=record)

    result_exporter = MagicMock()
    result_exporter.should_auto_export.return_value = True

    message = DummyMessage()

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=42,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        result_exporter=result_exporter,
    )
    if task is not None:
        await task

    # should_auto_export should NOT be called on failure
    result_exporter.should_auto_export.assert_not_called()
    assert len(message._documents) == 0


@pytest.mark.asyncio
async def test_auto_export_error_is_non_blocking(tmp_path: Path) -> None:
    """If auto-export raises an exception, it should not break the streamer."""
    record = _make_record(output_chars=5000)
    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
    ]
    task_service = DummyTaskService(events=events, status=record)

    result_exporter = MagicMock()
    result_exporter.should_auto_export.return_value = True
    result_exporter.export_markdown = AsyncMock(side_effect=RuntimeError("export boom"))

    message = DummyMessage()

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=42,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        result_exporter=result_exporter,
    )
    # Should not raise
    if task is not None:
        await task

    # Export was attempted but failed gracefully
    result_exporter.export_markdown.assert_awaited_once()
    assert len(message._documents) == 0


@pytest.mark.asyncio
async def test_auto_export_cleans_up_temp_file(tmp_path: Path) -> None:
    """After sending the document, the temp file should be cleaned up."""
    record = _make_record(output_chars=5000)
    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
    ]
    task_service = DummyTaskService(events=events, status=record)

    # Create a temp dir/file to verify cleanup
    export_dir = tmp_path / "export_tmp"
    export_dir.mkdir()
    md_file = export_dir / "task_t1.md"
    md_file.write_text("# Export", encoding="utf-8")
    export_result = ExportResult(file_path=md_file, filename="task_t1.md", mime_type="text/markdown")

    result_exporter = MagicMock()
    result_exporter.should_auto_export.return_value = True
    result_exporter.export_markdown = AsyncMock(return_value=export_result)

    message = DummyMessage()

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=42,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        result_exporter=result_exporter,
    )
    if task is not None:
        await task

    # File and its parent temp dir should be cleaned up
    assert not md_file.exists()


@pytest.mark.asyncio
async def test_auto_export_not_called_when_no_exporter() -> None:
    """When result_exporter is None, no export is attempted."""
    record = _make_record(output_chars=5000)
    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
    ]
    task_service = DummyTaskService(events=events, status=record)

    message = DummyMessage()

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=42,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp",
        # No result_exporter passed (defaults to None)
    )
    if task is not None:
        await task

    # No crash, no document
    assert len(message._documents) == 0
