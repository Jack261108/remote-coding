from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers.command_run import run_prompt_and_stream
from app.bot.presenters.chunk_sender import ChunkSender
from app.domain.file_models import FileUploadResult, FileValidationError
from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus
from app.services.upload_queue import UploadQueueManager
from tests.fakes.telegram import DummyMessage


class DummyTaskService:
    def __init__(self, events: list[CLIEvent], status: TaskRecord | None = None) -> None:
        self._events = events
        self._status = status
        self._revision = 0

    async def create_and_run(self, *, user_id: int, provider: str | None, prompt: str, workdir: str | None = None):
        task = SimpleNamespace(
            task_id="t1",
            provider="claude_code",
            session_id="s1",
            workdir=workdir or "/tmp/work",
            started_at=None,
            created_at=None,
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
        return False

    async def _stream(self):
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_queued_upload_scheduler_runs_after_success_message_is_displayed() -> None:
    message = DummyMessage(user_id=7)
    task_service = DummyTaskService(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="t1"),
            CLIEvent(type=EventType.EXITED, task_id="t1", exit_code=0),
        ],
        status=TaskRecord(
            task_id="t1",
            session_id="s1",
            user_id=7,
            provider="claude_code",
            prompt="hello",
            workdir="/tmp/work",
            timeout_sec=30,
            status=TaskStatus.SUCCEEDED,
        ),
    )
    scheduler_calls: list[tuple[int, str]] = []

    def queued_upload_scheduler(root_message: DummyMessage, user_id: int) -> None:
        scheduler_calls.append((user_id, root_message.sent_messages[0].text))

    task = await run_prompt_and_stream(
        message=message,
        task_service=task_service,
        sender_factory=lambda: ChunkSender(chunk_size=50, flush_interval_sec=0.01),
        user_id=message.from_user.id,
        provider="claude_code",
        prompt="hello",
        workdir="/tmp/work",
        queued_upload_scheduler=queued_upload_scheduler,
    )
    assert task is not None
    await task

    assert len(scheduler_calls) == 1
    called_user_id, displayed_text = scheduler_calls[0]
    assert called_user_id == 7
    assert "✅ 完成" in displayed_text


@pytest.mark.asyncio
async def test_queued_upload_processing_continues_after_failed_file(tmp_path: Path) -> None:
    from app.bot.handlers.file_upload import schedule_pending_upload_processing

    user_id = 7
    message = DummyMessage(user_id=user_id)
    upload_queue = UploadQueueManager(max_files_per_user=2, max_bytes_per_user=100)
    await upload_queue.enqueue(user_id=user_id, filename="bad.exe", data=b"bad")
    await upload_queue.enqueue(user_id=user_id, filename="good.txt", data=b"good")

    session_service = AsyncMock()
    session_service.get = AsyncMock(return_value=SimpleNamespace(workdir=str(tmp_path)))
    file_receiver = AsyncMock()
    file_receiver.receive_file = AsyncMock(
        side_effect=[
            FileValidationError(filename="bad.exe", reason="Extension .exe is not allowed."),
            FileUploadResult(filename="good.txt", size_bytes=4, path=tmp_path / "good.txt"),
        ]
    )

    task = schedule_pending_upload_processing(
        message,
        file_receiver=file_receiver,
        session_service=session_service,
        upload_queue=upload_queue,
        user_id=user_id,
    )
    await task

    assert message.answers == [
        "❌ 文件被拒绝: bad.exe\n原因: Extension .exe is not allowed.",
        "✅ 文件已接收: good.txt (4 B)",
    ]
    assert await upload_queue.queued_count(user_id=user_id) == 0
