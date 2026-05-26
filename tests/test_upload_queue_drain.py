"""Tests for upload queue drain after task completion (RunEventStreamer integration)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.file_models import FileUploadResult
from app.services.upload_queue_manager import UploadQueueManager


@pytest.fixture
def upload_queue() -> UploadQueueManager:
    return UploadQueueManager(max_files_per_user=5, max_bytes_per_user=100 * 1024 * 1024)


@pytest.fixture
def file_receiver() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def session_service() -> AsyncMock:
    svc = AsyncMock()
    session = MagicMock()
    session.workdir = "/tmp/work"
    svc.get = AsyncMock(return_value=session)
    return svc


@pytest.fixture
def messenger() -> AsyncMock:
    m = AsyncMock()
    m.send_message_safely = AsyncMock()
    return m


def _make_streamer(
    *,
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
    messenger: AsyncMock,
    user_id: int = 42,
):
    """Create a minimal RunEventStreamer for testing drain logic."""
    from app.bot.handlers.run_event_streamer import RunEventStreamer

    streamer = object.__new__(RunEventStreamer)
    streamer._upload_queue = upload_queue
    streamer._file_receiver = file_receiver
    streamer._session_service = session_service
    streamer._messenger = messenger
    streamer._user_id = user_id
    return streamer


@pytest.mark.asyncio
async def test_drain_processes_all_queued_files(
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
    messenger: AsyncMock,
) -> None:
    """After task completion, drain processes all queued files in FIFO order."""
    upload_queue.enqueue(42, "first.py", b"aaa", 3)
    upload_queue.enqueue(42, "second.py", b"bbb", 3)

    file_receiver.receive_file = AsyncMock(
        side_effect=[
            FileUploadResult(filename="first.py", size_bytes=3, path=Path("/tmp/work/.tg-uploads/42/first.py")),
            FileUploadResult(filename="second.py", size_bytes=3, path=Path("/tmp/work/.tg-uploads/42/second.py")),
        ]
    )

    streamer = _make_streamer(
        upload_queue=upload_queue,
        file_receiver=file_receiver,
        session_service=session_service,
        messenger=messenger,
    )

    await streamer._process_queued_uploads(42)

    assert file_receiver.receive_file.await_count == 2
    # Check FIFO order
    calls = file_receiver.receive_file.await_args_list
    assert calls[0].kwargs["filename"] == "first.py"
    assert calls[1].kwargs["filename"] == "second.py"
    # Confirmation messages sent
    assert messenger.send_message_safely.await_count == 2


@pytest.mark.asyncio
async def test_failed_file_does_not_block_subsequent(
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
    messenger: AsyncMock,
) -> None:
    """A failed file in the queue should not prevent subsequent files from processing."""
    upload_queue.enqueue(42, "fail.py", b"bad", 3)
    upload_queue.enqueue(42, "good.py", b"ok", 2)

    file_receiver.receive_file = AsyncMock(
        side_effect=[
            Exception("disk full"),
            FileUploadResult(filename="good.py", size_bytes=2, path=Path("/tmp/work/.tg-uploads/42/good.py")),
        ]
    )

    streamer = _make_streamer(
        upload_queue=upload_queue,
        file_receiver=file_receiver,
        session_service=session_service,
        messenger=messenger,
    )

    await streamer._process_queued_uploads(42)

    # Both files were attempted
    assert file_receiver.receive_file.await_count == 2
    # Messages: one error for fail.py, one success for good.py
    calls = messenger.send_message_safely.await_args_list
    assert any("fail.py" in str(c) for c in calls)
    assert any("good.py" in str(c) for c in calls)


@pytest.mark.asyncio
async def test_drain_no_op_when_queue_empty(
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
    messenger: AsyncMock,
) -> None:
    """Drain does nothing when there are no queued files."""
    streamer = _make_streamer(
        upload_queue=upload_queue,
        file_receiver=file_receiver,
        session_service=session_service,
        messenger=messenger,
    )

    await streamer._process_queued_uploads(42)

    file_receiver.receive_file.assert_not_awaited()
    messenger.send_message_safely.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_creates_background_task(
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
    messenger: AsyncMock,
) -> None:
    """_schedule_queued_upload_processing creates an asyncio task that drains the queue."""
    upload_queue.enqueue(42, "scheduled.py", b"data", 4)

    file_receiver.receive_file = AsyncMock(
        return_value=FileUploadResult(filename="scheduled.py", size_bytes=4, path=Path("/tmp/work/.tg-uploads/42/scheduled.py"))
    )

    streamer = _make_streamer(
        upload_queue=upload_queue,
        file_receiver=file_receiver,
        session_service=session_service,
        messenger=messenger,
    )

    streamer._schedule_queued_upload_processing()

    # Let the scheduled task run
    await asyncio.sleep(0.05)

    file_receiver.receive_file.assert_awaited_once()
    messenger.send_message_safely.assert_awaited_once()
