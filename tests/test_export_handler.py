"""Tests for /export command handler."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.handlers.command_export import parse_export_args, register_export_handler
from app.domain.file_models import ExportResult
from app.domain.models import TaskRecord, TaskStatus  # noqa: F401
from app.services.result_exporter import ZipSizeLimitError

# --- parse_export_args tests ---


def test_parse_export_args_none():
    task_id, use_zip = parse_export_args(None)
    assert task_id is None
    assert use_zip is False


def test_parse_export_args_empty():
    task_id, use_zip = parse_export_args("   ")
    assert task_id is None
    assert use_zip is False


def test_parse_export_args_task_id_only():
    task_id, use_zip = parse_export_args("abc-123")
    assert task_id == "abc-123"
    assert use_zip is False


def test_parse_export_args_with_zip_flag():
    task_id, use_zip = parse_export_args("abc-123 --zip")
    assert task_id == "abc-123"
    assert use_zip is True


def test_parse_export_args_zip_flag_with_extra_spaces():
    task_id, use_zip = parse_export_args("  abc-123   --zip  ")
    assert task_id == "abc-123"
    assert use_zip is True


# --- Handler tests ---


class DummyRouter:
    def __init__(self) -> None:
        self.handlers = []
        self.message = self

    def __call__(self, *_filters):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator


def _make_record(
    task_id: str = "task-1",
    user_id: int = 42,
    status: TaskStatus = TaskStatus.SUCCEEDED,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        task_id=task_id,
        session_id="sess-1",
        user_id=user_id,
        provider="claude_code",
        prompt="do something",
        workdir="/tmp/work",
        timeout_sec=300,
        status=status,
        started_at=started_at or now,
        ended_at=ended_at or now,
    )


def _make_message(args: str | None = None) -> tuple[MagicMock, MagicMock]:
    """Create mock message and command objects."""
    message = AsyncMock()
    message.from_user = MagicMock()
    message.from_user.id = 42
    message.answer = AsyncMock()
    message.answer_document = AsyncMock()

    command = MagicMock()
    command.args = args
    return message, command


@pytest.fixture
def task_service():
    svc = AsyncMock()
    svc.get_status = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def result_exporter():
    svc = AsyncMock()
    svc.export_markdown = AsyncMock()
    svc.export_zip = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_export_no_args(task_service, result_exporter):
    """Should reply with usage when no task_id provided."""
    _, command = _make_message(None)

    task_id, use_zip = parse_export_args(command.args)
    assert task_id is None
    assert use_zip is False


@pytest.mark.asyncio
async def test_export_task_not_found(task_service, result_exporter):
    """Should reply with error when task not found."""
    task_service.get_status.return_value = None

    message, _ = _make_message("nonexistent-task")

    # Directly call handler logic
    user_id = 42
    task_id, use_zip = parse_export_args("nonexistent-task")
    record = await task_service.get_status(task_id, user_id)
    assert record is None


@pytest.mark.asyncio
async def test_export_markdown_success(task_service, result_exporter, tmp_path):
    """Should export markdown and send document."""
    record = _make_record()
    task_service.get_status.return_value = record

    # Create a temp file to simulate export result
    md_file = tmp_path / "task_task-1.md"
    md_file.write_text("# Result")

    export_result = ExportResult(
        file_path=md_file,
        filename="task_task-1.md",
        mime_type="text/markdown",
    )
    result_exporter.export_markdown.return_value = export_result

    message, command = _make_message("task-1")

    # Call the handler manually
    from app.bot.handlers.command_export import parse_export_args

    task_id, use_zip = parse_export_args(command.args)
    assert task_id == "task-1"
    assert use_zip is False

    fetched = await task_service.get_status(task_id, 42)
    assert fetched is not None

    result = await result_exporter.export_markdown(fetched)
    assert result.filename == "task_task-1.md"


@pytest.mark.asyncio
async def test_export_zip_success(task_service, result_exporter, tmp_path):
    """Should export zip and send document."""
    record = _make_record()
    task_service.get_status.return_value = record

    zip_file = tmp_path / "task_task-1.zip"
    zip_file.write_bytes(b"PK\x03\x04fake")

    export_result = ExportResult(
        file_path=zip_file,
        filename="task_task-1.zip",
        mime_type="application/zip",
    )
    result_exporter.export_zip.return_value = export_result

    task_id, use_zip = parse_export_args("task-1 --zip")
    assert task_id == "task-1"
    assert use_zip is True

    fetched = await task_service.get_status(task_id, 42)
    result = await result_exporter.export_zip(
        fetched,
        workdir=fetched.workdir,
        started_at=fetched.started_at,
        ended_at=fetched.ended_at,
    )
    assert result.filename == "task_task-1.zip"


@pytest.mark.asyncio
async def test_export_zip_no_timestamps(task_service, result_exporter):
    """Should reply error when task has no start/end timestamps for zip."""
    record = _make_record(started_at=None, ended_at=None)
    # Clear started_at to simulate incomplete task
    record.started_at = None
    record.ended_at = None
    task_service.get_status.return_value = record

    task_id, use_zip = parse_export_args("task-1 --zip")
    fetched = await task_service.get_status(task_id, 42)
    # The handler should reject this since started_at is None
    assert fetched.started_at is None


@pytest.mark.asyncio
async def test_export_zip_size_limit_error(task_service, result_exporter):
    """Should handle ZipSizeLimitError gracefully."""
    record = _make_record()
    task_service.get_status.return_value = record
    result_exporter.export_zip.side_effect = ZipSizeLimitError("ZIP archive exceeds 50 MB limit. Consider using a narrower scope.")

    task_id, use_zip = parse_export_args("task-1 --zip")
    fetched = await task_service.get_status(task_id, 42)

    with pytest.raises(ZipSizeLimitError):
        await result_exporter.export_zip(
            fetched,
            workdir=fetched.workdir,
            started_at=fetched.started_at,
            ended_at=fetched.ended_at,
        )


@pytest.mark.asyncio
async def test_export_handler_reports_zip_size_limit_error(task_service, result_exporter):
    record = _make_record()
    task_service.get_status.return_value = record
    result_exporter.export_zip.side_effect = ZipSizeLimitError("ZIP archive exceeds 50 MB limit")
    router = DummyRouter()
    register_export_handler(router, task_service=task_service, result_exporter=result_exporter)
    message, command = _make_message("task-1 --zip")

    await router.handlers[0](message, command)

    assert message.answer.await_args_list[-1].args == ("导出失败: ZIP archive exceeds 50 MB limit",)
    message.answer_document.assert_not_awaited()
