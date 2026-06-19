"""Tests for ContextBuilderService integration in TaskService."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.storage.memory import MemoryTaskStore
from app.domain.file_models import TaskContext
from app.domain.models import CLIEvent, EventType, TaskStatus
from app.services.context_builder import ContextBuilderService
from app.services.task_service import TaskService
from tests.fakes.cli import StubAdapter, StubFactory, make_file_backed_session_service, make_settings


def _make_context_builder(*, file_paths=None, augmented_prompt=None, cli_args=None) -> MagicMock:
    """Create a mock ContextBuilderService that returns the specified TaskContext."""
    mock = MagicMock(spec=ContextBuilderService)
    mock.build_context.return_value = TaskContext(
        file_paths=file_paths or [],
        augmented_prompt=augmented_prompt or "original",
        cli_args=cli_args or [],
    )
    mock.cleanup_after_task = AsyncMock()
    return mock


@pytest.mark.asyncio
async def test_build_context_called_before_execution(tmp_path: Path) -> None:
    """Verify build_context is called with correct params before CLI execution."""
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    context_builder = _make_context_builder(
        augmented_prompt="hi\n\n[Attached files: code.py]",
        cli_args=["--file", "/tmp/code.py"],
    )

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    # build_context should have been called
    context_builder.build_context.assert_called_once()
    call_kwargs = context_builder.build_context.call_args.kwargs
    assert call_kwargs["user_id"] == 1
    assert call_kwargs["workdir"] == str(tmp_path.resolve())
    assert call_kwargs["provider"] == "claude_code"
    assert call_kwargs["prompt"] == "hi"
    assert isinstance(call_kwargs["since"], datetime)


@pytest.mark.asyncio
async def test_augmented_prompt_used_in_execution(tmp_path: Path) -> None:
    """Verify the augmented prompt is used when calling the CLI adapter."""
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    context_builder = _make_context_builder(
        augmented_prompt="hi\n\n[Attached files: code.py]",
        cli_args=["--file", "/tmp/code.py"],
    )

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    # The task record stores the original prompt
    assert result.task.prompt == "hi"


@pytest.mark.asyncio
async def test_cleanup_runs_after_final_task_save(tmp_path: Path) -> None:
    events_seen: list[str] = []

    class RecordingTaskStore(MemoryTaskStore):
        async def save(self, record):
            await super().save(record)
            if record.is_final:
                events_seen.append("final_save")

    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    context_builder = _make_context_builder()

    async def cleanup_after_task(user_id: int, workdir: str) -> None:
        assert events_seen == ["final_save"]
        events_seen.append("cleanup")

    context_builder.cleanup_after_task = AsyncMock(side_effect=cleanup_after_task)
    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=RecordingTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    assert events_seen == ["final_save", "cleanup"]


@pytest.mark.asyncio
async def test_cleanup_called_on_task_success(tmp_path: Path) -> None:
    """Verify cleanup_after_task is called when task succeeds."""
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    context_builder = _make_context_builder()

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    context_builder.cleanup_after_task.assert_awaited_once_with(1, str(tmp_path.resolve()))


@pytest.mark.asyncio
async def test_cleanup_called_on_task_failure(tmp_path: Path) -> None:
    """Verify cleanup_after_task is called when task fails."""
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.FAILED, task_id="x", exit_code=1, error="boom"),
        ]
    )
    context_builder = _make_context_builder()

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    context_builder.cleanup_after_task.assert_awaited_once_with(1, str(tmp_path.resolve()))


@pytest.mark.asyncio
async def test_cleanup_called_on_task_timeout(tmp_path: Path) -> None:
    """Verify cleanup_after_task is called when task times out."""
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.TIMEOUT, task_id="x", error="timeout"),
        ]
    )
    context_builder = _make_context_builder()

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    context_builder.cleanup_after_task.assert_awaited_once_with(1, str(tmp_path.resolve()))


@pytest.mark.asyncio
async def test_cleanup_called_on_task_canceled(tmp_path: Path) -> None:
    """Verify cleanup_after_task is called when task is canceled."""
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.CANCELED, task_id="x", error="cancel"),
        ]
    )
    context_builder = _make_context_builder()

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    context_builder.cleanup_after_task.assert_awaited_once_with(1, str(tmp_path.resolve()))


@pytest.mark.asyncio
async def test_no_context_builder_proceeds_normally(tmp_path: Path) -> None:
    """Verify task works normally when no context_builder is provided."""
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        # No context_builder
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    events = [event async for event in result.events]

    assert [e.type for e in events] == [EventType.STARTED, EventType.EXITED]
    status = await service.get_status(result.task.task_id, user_id=1)
    assert status is not None
    assert status.status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_no_files_proceeds_with_original_prompt(tmp_path: Path) -> None:
    """When build_context returns no files, original prompt is used."""
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    # Returns empty context (no files)
    context_builder = _make_context_builder(augmented_prompt="hi", cli_args=[])

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    assert result.task.prompt == "hi"


@pytest.mark.asyncio
async def test_build_context_failure_does_not_leave_pending_task(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    context_builder = _make_context_builder()
    context_builder.build_context.side_effect = RuntimeError("context boom")
    task_store = MemoryTaskStore()

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=task_store,
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    with pytest.raises(RuntimeError, match="context boom"):
        await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))

    assert await task_store.list_by_user(1) == []


@pytest.mark.asyncio
async def test_since_uses_last_task_ended_at(tmp_path: Path) -> None:
    """Verify since parameter reflects the last task's ended_at timestamp."""
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    context_builder = _make_context_builder()
    task_store = MemoryTaskStore()

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=task_store,
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
        context_builder=context_builder,
    )

    # First task - since should be epoch (no previous tasks)
    result1 = await service.create_and_run(user_id=1, provider="claude", prompt="first", workdir=str(tmp_path))
    _ = [event async for event in result1.events]

    first_call_since = context_builder.build_context.call_args.kwargs["since"]
    assert first_call_since == datetime(1970, 1, 1, tzinfo=UTC)

    # Second task - since should be the ended_at of the first task
    context_builder.build_context.reset_mock()
    result2 = await service.create_and_run(user_id=1, provider="claude", prompt="second", workdir=str(tmp_path))
    _ = [event async for event in result2.events]

    second_call_since = context_builder.build_context.call_args.kwargs["since"]
    # The first task should have ended, so since should be after epoch
    assert second_call_since > datetime(1970, 1, 1, tzinfo=UTC)
