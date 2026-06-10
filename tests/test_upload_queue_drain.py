"""Tests for upload queue drain after task completion."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers.file_upload import process_pending_uploads, schedule_pending_upload_processing
from app.domain.file_models import FileUploadResult, FileValidationError
from app.services.upload_queue import UploadQueueManager
from tests.fakes.telegram import DummyMessage


@pytest.fixture
def upload_queue() -> UploadQueueManager:
    return UploadQueueManager(max_files_per_user=5, max_bytes_per_user=100 * 1024 * 1024)


@pytest.fixture
def file_receiver() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def session_service(tmp_path: Path) -> AsyncMock:
    svc = AsyncMock()
    svc.get = AsyncMock(return_value=SimpleNamespace(workdir=str(tmp_path)))
    return svc


@pytest.mark.asyncio
async def test_drain_processes_all_queued_files(
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
) -> None:
    """After task completion, drain processes all queued files in FIFO order."""
    await upload_queue.enqueue(user_id=42, filename="first.py", data=b"aaa")
    await upload_queue.enqueue(user_id=42, filename="second.py", data=b"bbb")

    file_receiver.receive_file = AsyncMock(
        side_effect=[
            FileUploadResult(filename="first.py", size_bytes=3, path=Path("/tmp/work/.tg-uploads/42/first.py")),
            FileUploadResult(filename="second.py", size_bytes=3, path=Path("/tmp/work/.tg-uploads/42/second.py")),
        ]
    )
    message = DummyMessage(user_id=42)

    await process_pending_uploads(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        upload_queue=upload_queue,
        user_id=42,
    )

    assert file_receiver.receive_file.await_count == 2
    calls = file_receiver.receive_file.await_args_list
    assert calls[0].kwargs["filename"] == "first.py"
    assert calls[1].kwargs["filename"] == "second.py"
    assert message.answers == [
        "✅ 文件已接收: first.py (3 B)",
        "✅ 文件已接收: second.py (3 B)",
    ]


@pytest.mark.asyncio
async def test_drain_uses_workdir_captured_when_file_was_queued(
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
    tmp_path: Path,
) -> None:
    """Queued uploads are stored in the workdir active at upload time."""
    queued_workdir = tmp_path / "queued-workdir"
    current_workdir = tmp_path / "current-workdir"
    session_service.get = AsyncMock(return_value=SimpleNamespace(workdir=str(current_workdir)))

    await upload_queue.enqueue(user_id=42, filename="queued.py", data=b"aaa", workdir=str(queued_workdir))
    file_receiver.receive_file = AsyncMock(
        return_value=FileUploadResult(filename="queued.py", size_bytes=3, path=queued_workdir / ".tg-uploads" / "42" / "queued.py")
    )
    message = DummyMessage(user_id=42)

    await process_pending_uploads(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        upload_queue=upload_queue,
        user_id=42,
    )

    file_receiver.receive_file.assert_awaited_once_with(
        user_id=42,
        workdir=str(queued_workdir),
        filename="queued.py",
        data=b"aaa",
    )


@pytest.mark.asyncio
async def test_failed_file_does_not_block_subsequent(
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
) -> None:
    """A failed file in the queue should not prevent subsequent files from processing."""
    await upload_queue.enqueue(user_id=42, filename="fail.py", data=b"bad")
    await upload_queue.enqueue(user_id=42, filename="good.py", data=b"ok")

    file_receiver.receive_file = AsyncMock(
        side_effect=[
            FileValidationError(filename="fail.py", reason="invalid content"),
            FileUploadResult(filename="good.py", size_bytes=2, path=Path("/tmp/work/.tg-uploads/42/good.py")),
        ]
    )
    message = DummyMessage(user_id=42)

    await process_pending_uploads(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        upload_queue=upload_queue,
        user_id=42,
    )

    assert file_receiver.receive_file.await_count == 2
    assert message.answers == [
        "❌ 文件被拒绝: fail.py\n原因: invalid content",
        "✅ 文件已接收: good.py (2 B)",
    ]


@pytest.mark.asyncio
async def test_drain_no_op_when_queue_empty(
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
) -> None:
    """Drain does nothing when there are no queued files."""
    message = DummyMessage(user_id=42)

    await process_pending_uploads(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        upload_queue=upload_queue,
        user_id=42,
    )

    file_receiver.receive_file.assert_not_awaited()
    assert message.answers == []


@pytest.mark.asyncio
async def test_schedule_creates_background_task(
    upload_queue: UploadQueueManager,
    file_receiver: AsyncMock,
    session_service: AsyncMock,
) -> None:
    """schedule_pending_upload_processing creates an asyncio task that drains the queue."""
    await upload_queue.enqueue(user_id=42, filename="scheduled.py", data=b"data")

    file_receiver.receive_file = AsyncMock(
        return_value=FileUploadResult(filename="scheduled.py", size_bytes=4, path=Path("/tmp/work/.tg-uploads/42/scheduled.py"))
    )
    message = DummyMessage(user_id=42)

    task = schedule_pending_upload_processing(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        upload_queue=upload_queue,
        user_id=42,
    )
    await task

    file_receiver.receive_file.assert_awaited_once()
    assert message.answers == ["✅ 文件已接收: scheduled.py (4 B)"]
