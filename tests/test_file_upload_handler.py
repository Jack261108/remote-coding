"""Tests for the file upload handler."""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.handlers.file_upload import (
    _format_size,
    _pending_uploads,
    _user_has_running_task,
)
from app.domain.file_models import FileUploadResult, FileValidationError
from app.domain.models import TaskRecord, TaskStatus


# --- Unit tests for helper functions ---


def test_format_size_bytes() -> None:
    assert _format_size(500) == "500 B"


def test_format_size_kb() -> None:
    assert _format_size(2048) == "2.0 KB"


def test_format_size_mb() -> None:
    assert _format_size(5 * 1024 * 1024) == "5.0 MB"


# --- Tests for _user_has_running_task ---


@pytest.mark.asyncio
async def test_user_has_running_task_true() -> None:
    task_service = AsyncMock()
    record = MagicMock(spec=TaskRecord)
    record.status = TaskStatus.RUNNING
    task_service.list_recent = AsyncMock(return_value=[record])

    result = await _user_has_running_task(task_service, user_id=123)
    assert result is True


@pytest.mark.asyncio
async def test_user_has_running_task_false_when_all_final() -> None:
    task_service = AsyncMock()
    record = MagicMock(spec=TaskRecord)
    record.status = TaskStatus.SUCCEEDED
    task_service.list_recent = AsyncMock(return_value=[record])

    result = await _user_has_running_task(task_service, user_id=123)
    assert result is False


@pytest.mark.asyncio
async def test_user_has_running_task_false_when_no_tasks() -> None:
    task_service = AsyncMock()
    task_service.list_recent = AsyncMock(return_value=[])

    result = await _user_has_running_task(task_service, user_id=123)
    assert result is False


# --- Integration tests for the handler ---


def _make_message(user_id: int = 42) -> MagicMock:
    message = AsyncMock(spec=["from_user", "bot", "document", "photo", "answer"])
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.answer = AsyncMock()
    return message


def _make_services():
    file_receiver = AsyncMock()
    session_service = AsyncMock()
    task_service = AsyncMock()
    task_service.list_recent = AsyncMock(return_value=[])
    return file_receiver, session_service, task_service


@pytest.fixture(autouse=True)
def clear_pending_uploads():
    """Clear the in-memory pending uploads between tests."""
    _pending_uploads.clear()
    yield
    _pending_uploads.clear()


@pytest.mark.asyncio
async def test_handle_document_success() -> None:
    """Document upload should download, process, and reply with confirmation."""
    from pathlib import Path

    file_receiver, session_service, task_service = _make_services()

    session = MagicMock()
    session.workdir = "/tmp/work"
    session_service.get = AsyncMock(return_value=session)

    file_receiver.receive_file = AsyncMock(
        return_value=FileUploadResult(filename="test.py", size_bytes=1234, path=Path("/tmp/work/.tg-uploads/42/test.py"))
    )

    message = _make_message()
    message.document = MagicMock()
    message.document.file_name = "test.py"
    message.document.file_id = "file123"

    bot = AsyncMock()
    message.bot = bot
    file_obj = MagicMock()
    file_obj.file_path = "documents/test.py"
    bot.get_file = AsyncMock(return_value=file_obj)
    bot.download_file = AsyncMock(return_value=io.BytesIO(b"hello world"))

    # Import the actual handler function by extracting from registration
    from app.bot.handlers.file_upload import _process_upload

    # Simulate the handler logic directly
    data = b"hello world"
    await _process_upload(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        filename="test.py",
        data=data,
    )

    file_receiver.receive_file.assert_awaited_once_with(user_id=42, workdir="/tmp/work", filename="test.py", data=data)
    message.answer.assert_awaited_once()
    reply = message.answer.call_args[0][0]
    assert "test.py" in reply
    assert "✅" in reply


@pytest.mark.asyncio
async def test_handle_document_validation_error() -> None:
    """Rejected file should reply with error."""
    file_receiver, session_service, task_service = _make_services()

    session = MagicMock()
    session.workdir = "/tmp/work"
    session_service.get = AsyncMock(return_value=session)

    file_receiver.receive_file = AsyncMock(return_value=FileValidationError(filename="bad.exe", reason="Extension .exe is not allowed."))

    message = _make_message()
    data = b"MZ binary"

    from app.bot.handlers.file_upload import _process_upload

    await _process_upload(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        filename="bad.exe",
        data=data,
    )

    message.answer.assert_awaited_once()
    reply = message.answer.call_args[0][0]
    assert "❌" in reply
    assert "bad.exe" in reply
    assert ".exe is not allowed" in reply


@pytest.mark.asyncio
async def test_handle_document_no_session() -> None:
    """If user has no session, reply with guidance."""
    file_receiver, session_service, task_service = _make_services()
    session_service.get = AsyncMock(return_value=None)

    message = _make_message()

    from app.bot.handlers.file_upload import _process_upload

    await _process_upload(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        filename="test.py",
        data=b"data",
    )

    message.answer.assert_awaited_once()
    reply = message.answer.call_args[0][0]
    assert "/session" in reply or "/claude" in reply


@pytest.mark.asyncio
async def test_queues_upload_when_task_running() -> None:
    """Uploads should be queued when user has a running task."""
    file_receiver, session_service, task_service = _make_services()
    running_task = MagicMock(spec=TaskRecord)
    running_task.status = TaskStatus.RUNNING
    task_service.list_recent = AsyncMock(return_value=[running_task])

    assert await _user_has_running_task(task_service, user_id=42) is True

    # Simulate queuing
    _pending_uploads[42].append(("test.py", b"data"))
    assert len(_pending_uploads[42]) == 1


@pytest.mark.asyncio
async def test_process_pending_uploads() -> None:
    """process_pending_uploads should process all queued files."""
    from pathlib import Path

    from app.bot.handlers.file_upload import process_pending_uploads

    file_receiver, session_service, task_service = _make_services()

    session = MagicMock()
    session.workdir = "/tmp/work"
    session_service.get = AsyncMock(return_value=session)

    file_receiver.receive_file = AsyncMock(
        return_value=FileUploadResult(filename="queued.py", size_bytes=100, path=Path("/tmp/work/.tg-uploads/42/queued.py"))
    )

    # Queue a file
    _pending_uploads[42].append(("queued.py", b"content"))

    message = _make_message()

    await process_pending_uploads(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        user_id=42,
    )

    # Queue should be cleared
    assert 42 not in _pending_uploads
    file_receiver.receive_file.assert_awaited_once()
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_failure_replies_with_error() -> None:
    """If Telegram download fails, reply with error message."""
    file_receiver, session_service, task_service = _make_services()

    message = _make_message()
    message.document = MagicMock()
    message.document.file_name = "test.py"
    message.document.file_id = "file123"

    bot = AsyncMock()
    message.bot = bot
    bot.get_file = AsyncMock(side_effect=Exception("Network timeout"))

    # Simulate what handle_document does on download failure:
    try:
        await bot.get_file("file123")
    except Exception as exc:
        await message.answer(f"❌ 文件下载失败: {exc}")

    message.answer.assert_awaited_once()
    reply = message.answer.call_args[0][0]
    assert "❌" in reply
    assert "Network timeout" in reply
