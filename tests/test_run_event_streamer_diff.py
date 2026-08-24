"""Tests for DiffGeneratorService integration in RunEventStreamer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.bot.handlers.command_run import run_prompt_and_stream
from app.bot.presenters.chunk_sender import ChunkSender
from app.domain.file_models import DiffResult
from app.domain.models import TaskStatus
from app.services.background_task_registry import BackgroundTaskRegistry
from app.services.diff_generator import DiffGeneratorService
from tests.fakes.task_service import FakeTaskService, make_cli_event_stream, make_task_record
from tests.fakes.telegram import DummyMessage


def _task_service(tmp_path: Path, *, status: TaskStatus, failed: bool = False) -> FakeTaskService:
    return FakeTaskService(
        events=make_cli_event_stream(failed=failed, error="something broke" if failed else None),
        status=make_task_record(user_id=1, prompt="test", workdir=str(tmp_path), timeout_sec=60, status=status, output_chars=10),
        wait_update_result=False,
        wait_update_sleep=0.01,
    )


@pytest.fixture
def stream_background_tasks() -> BackgroundTaskRegistry:
    return BackgroundTaskRegistry(label="stream")


@pytest.mark.asyncio
async def test_diff_integration_sends_short_diff_as_message(tmp_path: Path, stream_background_tasks: BackgroundTaskRegistry) -> None:
    """When diff is short (<4096), it should be sent as a code-block message."""
    diff_generator = DiffGeneratorService()
    snapshot = {tmp_path / "file.py": 100.0}
    small_diff = DiffResult(content="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new", file_count=1, is_patch_file=False)

    task_service = _task_service(tmp_path, status=TaskStatus.SUCCEEDED)
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
            stream_background_tasks=stream_background_tasks,
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
async def test_diff_integration_sends_large_diff_as_patch_file(tmp_path: Path, stream_background_tasks: BackgroundTaskRegistry) -> None:
    """When diff is large (>=4096), it should be sent as a .patch file."""
    diff_generator = DiffGeneratorService()
    snapshot = {tmp_path / "file.py": 100.0}
    large_content = "x" * 5000
    large_diff = DiffResult(content=large_content, file_count=3, is_patch_file=True)

    task_service = _task_service(tmp_path, status=TaskStatus.SUCCEEDED)
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
            stream_background_tasks=stream_background_tasks,
        )
        if task:
            await task

    # Check that a document was sent
    assert len(message.sent_documents) >= 1
    doc = message.sent_documents[0]
    assert doc["filename"].endswith(".patch")


@pytest.mark.asyncio
async def test_diff_integration_no_diff_when_no_changes(tmp_path: Path, stream_background_tasks: BackgroundTaskRegistry) -> None:
    """When generate_unified_diff returns None, no diff message should be sent."""
    diff_generator = DiffGeneratorService()
    snapshot = {tmp_path / "file.py": 100.0}

    task_service = _task_service(tmp_path, status=TaskStatus.SUCCEEDED)
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
            stream_background_tasks=stream_background_tasks,
        )
        if task:
            await task

    # Only lifecycle messages, no diff
    sent_texts = message.answers
    assert not any("diff" in t.lower() for t in sent_texts if "处理中" not in t and "完成" not in t)


@pytest.mark.asyncio
async def test_diff_integration_error_does_not_block_task(tmp_path: Path, stream_background_tasks: BackgroundTaskRegistry) -> None:
    """If diff generation raises an exception, the task should still complete."""
    diff_generator = DiffGeneratorService()

    task_service = _task_service(tmp_path, status=TaskStatus.SUCCEEDED)
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
            stream_background_tasks=stream_background_tasks,
        )
        if task:
            await task

    # Task should still complete without raising — the stream task finishes normally
    _ = message.answers
    # Success message is typically edited into the lifecycle message, not sent as new answer
    # The key assertion is that no exception propagated and the task completed
    assert task is not None  # Task was created and completed without error


@pytest.mark.asyncio
async def test_diff_not_triggered_on_failure(tmp_path: Path, stream_background_tasks: BackgroundTaskRegistry) -> None:
    """Diff should NOT be generated when task fails."""
    diff_generator = DiffGeneratorService()
    snapshot = {tmp_path / "file.py": 100.0}

    task_service = _task_service(tmp_path, status=TaskStatus.FAILED, failed=True)
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
            stream_background_tasks=stream_background_tasks,
        )
        if task:
            await task

    # Snapshot captured at start, but no diff generated
    mock_snap.assert_called_once()
    mock_detect.assert_not_called()
    mock_gen.assert_not_called()
