"""Tests for DiffGeneratorService integration in RunEventStreamer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.bot.handlers.command_run import run_prompt_and_stream
from app.bot.presenters.chunk_sender import ChunkSender
from app.domain.file_models import DiffResult
from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus
from app.services.diff_generator import DiffGeneratorService
from tests.fakes.telegram import DummyMessage


class DummyTaskService:
    def __init__(self, events: list[CLIEvent], status: TaskRecord | None = None) -> None:
        self._events = events
        self._status = status
        self._revision = 0

    async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
        task = SimpleNamespace(
            task_id="t1", provider="claude_code", session_id="s1", workdir=workdir or "/tmp/work", started_at=None, created_at=None
        )
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

    async def wait_for_structured_session_update(self, **kwargs) -> bool:
        await asyncio.sleep(0.01)
        return False

    async def _stream(self):
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_diff_integration_sends_short_diff_as_message(tmp_path: Path) -> None:
    """When diff is short (<4096), it should be sent as a code-block message."""
    diff_generator = DiffGeneratorService()
    snapshot = {tmp_path / "file.py": 100.0}
    small_diff = DiffResult(content="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new", file_count=1, is_patch_file=False)

    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
    ]
    status = TaskRecord(
        task_id="t1",
        session_id="s1",
        user_id=1,
        provider="claude_code",
        prompt="test",
        workdir=str(tmp_path),
        timeout_sec=60,
        status=TaskStatus.SUCCEEDED,
        output_chars=10,
    )
    task_service = DummyTaskService(events=events, status=status)
    message = DummyMessage()

    with (
        patch.object(diff_generator, "capture_snapshot", return_value=snapshot) as mock_snap,
        patch.object(diff_generator, "detect_modified_files", return_value=[tmp_path / "file.py"]) as mock_detect,
        patch.object(diff_generator, "generate_unified_diff", return_value=small_diff) as mock_gen,
    ):
        task = await run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
            user_id=1,
            provider="claude_code",
            prompt="hello",
            workdir=str(tmp_path),
            diff_generator=diff_generator,
        )
        if task:
            await task

    mock_snap.assert_called_once()
    mock_detect.assert_called_once()
    mock_gen.assert_called_once()

    # Check that a message containing the diff was sent
    sent_texts = message.answers
    diff_sent = any("```diff" in t or "--- a/file.py" in t for t in sent_texts)
    assert diff_sent, f"Expected diff message in sent texts: {sent_texts}"


@pytest.mark.asyncio
async def test_diff_integration_sends_large_diff_as_patch_file(tmp_path: Path) -> None:
    """When diff is large (>=4096), it should be sent as a .patch file."""
    diff_generator = DiffGeneratorService()
    snapshot = {tmp_path / "file.py": 100.0}
    large_content = "x" * 5000
    large_diff = DiffResult(content=large_content, file_count=3, is_patch_file=True)

    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
    ]
    status = TaskRecord(
        task_id="t1",
        session_id="s1",
        user_id=1,
        provider="claude_code",
        prompt="test",
        workdir=str(tmp_path),
        timeout_sec=60,
        status=TaskStatus.SUCCEEDED,
        output_chars=10,
    )
    task_service = DummyTaskService(events=events, status=status)
    message = DummyMessage()

    with (
        patch.object(diff_generator, "capture_snapshot", return_value=snapshot),
        patch.object(diff_generator, "detect_modified_files", return_value=[tmp_path / "file.py"]),
        patch.object(diff_generator, "generate_unified_diff", return_value=large_diff),
    ):
        task = await run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
            user_id=1,
            provider="claude_code",
            prompt="hello",
            workdir=str(tmp_path),
            diff_generator=diff_generator,
        )
        if task:
            await task

    # Check that a document was sent
    assert len(message.sent_documents) >= 1
    doc = message.sent_documents[0]
    assert doc["filename"].endswith(".patch")


@pytest.mark.asyncio
async def test_diff_integration_no_diff_when_no_changes(tmp_path: Path) -> None:
    """When generate_unified_diff returns None, no diff message should be sent."""
    diff_generator = DiffGeneratorService()
    snapshot = {tmp_path / "file.py": 100.0}

    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
    ]
    status = TaskRecord(
        task_id="t1",
        session_id="s1",
        user_id=1,
        provider="claude_code",
        prompt="test",
        workdir=str(tmp_path),
        timeout_sec=60,
        status=TaskStatus.SUCCEEDED,
        output_chars=10,
    )
    task_service = DummyTaskService(events=events, status=status)
    message = DummyMessage()

    with (
        patch.object(diff_generator, "capture_snapshot", return_value=snapshot),
        patch.object(diff_generator, "detect_modified_files", return_value=[]),
        patch.object(diff_generator, "generate_unified_diff", return_value=None),
    ):
        task = await run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
            user_id=1,
            provider="claude_code",
            prompt="hello",
            workdir=str(tmp_path),
            diff_generator=diff_generator,
        )
        if task:
            await task

    # Only lifecycle messages, no diff
    sent_texts = message.answers
    assert not any("diff" in t.lower() for t in sent_texts if "处理中" not in t and "完成" not in t)


@pytest.mark.asyncio
async def test_diff_integration_error_does_not_block_task(tmp_path: Path) -> None:
    """If diff generation raises an exception, the task should still complete."""
    diff_generator = DiffGeneratorService()

    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
    ]
    status = TaskRecord(
        task_id="t1",
        session_id="s1",
        user_id=1,
        provider="claude_code",
        prompt="test",
        workdir=str(tmp_path),
        timeout_sec=60,
        status=TaskStatus.SUCCEEDED,
        output_chars=10,
    )
    task_service = DummyTaskService(events=events, status=status)
    message = DummyMessage()

    with patch.object(diff_generator, "capture_snapshot", side_effect=OSError("disk error")):
        task = await run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
            user_id=1,
            provider="claude_code",
            prompt="hello",
            workdir=str(tmp_path),
            diff_generator=diff_generator,
        )
        if task:
            await task

    # Task should still complete without raising — the stream task finishes normally
    _ = message.answers
    # Success message is typically edited into the lifecycle message, not sent as new answer
    # The key assertion is that no exception propagated and the task completed
    assert task is not None  # Task was created and completed without error


@pytest.mark.asyncio
async def test_diff_not_triggered_on_failure(tmp_path: Path) -> None:
    """Diff should NOT be generated when task fails."""
    diff_generator = DiffGeneratorService()
    snapshot = {tmp_path / "file.py": 100.0}

    events = [
        CLIEvent(type=EventType.STARTED, task_id="t1"),
        CLIEvent(type=EventType.FAILED, task_id="t1", error="something broke"),
    ]
    status = TaskRecord(
        task_id="t1",
        session_id="s1",
        user_id=1,
        provider="claude_code",
        prompt="test",
        workdir=str(tmp_path),
        timeout_sec=60,
        status=TaskStatus.FAILED,
        output_chars=10,
    )
    task_service = DummyTaskService(events=events, status=status)
    message = DummyMessage()

    with (
        patch.object(diff_generator, "capture_snapshot", return_value=snapshot) as mock_snap,
        patch.object(diff_generator, "detect_modified_files") as mock_detect,
        patch.object(diff_generator, "generate_unified_diff") as mock_gen,
    ):
        task = await run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
            user_id=1,
            provider="claude_code",
            prompt="hello",
            workdir=str(tmp_path),
            diff_generator=diff_generator,
        )
        if task:
            await task

    # Snapshot captured at start, but no diff generated
    mock_snap.assert_called_once()
    mock_detect.assert_not_called()
    mock_gen.assert_not_called()
