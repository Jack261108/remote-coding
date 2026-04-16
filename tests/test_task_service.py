import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.adapters.storage.file_session_store import FileSessionStore
from app.adapters.storage.memory import MemoryTaskStore
from app.config.settings import Settings
from app.domain.models import CLIEvent, EventType, ExecutionTask, TaskStatus
from app.services.session_service import SessionService
from app.services.task_service import TaskService


def expected_terminal_id(*, user_id: int, workdir: str) -> str:
    return SessionService(store=None)._build_terminal_id(user_id=user_id, workdir=workdir)


class StubAdapter(BaseCLIAdapter):
    provider = "stub"

    def __init__(self, events: list[CLIEvent]) -> None:
        self._events = events
        self.cancel_called = False
        self.last_terminal_key: str | None = None
        self.last_interactive: bool = False
        self.last_claude_session_id: str | None = None

    async def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ) -> AsyncIterator[CLIEvent]:
        self.last_terminal_key = terminal_key
        self.last_interactive = interactive
        self.last_claude_session_id = claude_session_id
        for event in self._events:
            await asyncio.sleep(0)
            yield CLIEvent(
                type=event.type,
                task_id=task.task_id,
                content=event.content,
                exit_code=event.exit_code,
                error=event.error,
            )

    async def cancel(self, task_id: str) -> bool:
        self.cancel_called = True
        return True


class StubFactory:
    def __init__(self, adapter: BaseCLIAdapter) -> None:
        self._adapters = {"claude_code": adapter, "codex": adapter, "gemini": adapter}
        self._closed_terminal_key: str | None = None
        self._ensured_terminal_key: str | None = None
        self._ensured_workdir: str | None = None
        self._ensured_interactive_terminal_key: str | None = None
        self._ensured_interactive_workdir: str | None = None
        self._revealed_terminal_key: str | None = None

    def normalize_provider(self, provider: str) -> str:
        p = provider.strip().lower()
        if p in {"claude", "claude_code", "claude-code"}:
            return "claude_code"
        if p in {"codex", "codex_cli", "codex-cli"}:
            return "codex"
        if p in {"gemini", "gemini_cli", "gemini-cli"}:
            return "gemini"
        raise ValueError("不支持 provider")

    def get(self, provider: str) -> BaseCLIAdapter:
        return self._adapters[self.normalize_provider(provider)]

    def available_providers(self) -> list[str]:
        return ["claude_code", "codex", "gemini"]

    async def close_terminal(self, terminal_key: str) -> bool:
        self._closed_terminal_key = terminal_key
        return True

    async def ensure_terminal(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        self._ensured_terminal_key = terminal_key
        self._ensured_workdir = workdir
        return True, ""

    async def ensure_claude_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        self._ensured_interactive_terminal_key = terminal_key
        self._ensured_interactive_workdir = workdir
        return True, ""

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        self._revealed_terminal_key = terminal_key
        return True, f"已在桌面打开 Terminal 并附着到 tgcli_{terminal_key}"


def make_settings(tmp_path: Path, *, claude_tmux_mode: bool = False) -> Settings:
    return Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 2,
            "CLAUDE_TMUX_MODE": claude_tmux_mode,
            "CLAUDE_CLI_BIN": "claude",
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": str(tmp_path),
            "TASK_OUTPUT_CHAR_LIMIT": 20,
        }
    )


def make_file_backed_session_service(tmp_path: Path) -> SessionService:
    return SessionService(FileSessionContextStore(FileSessionStore(str(tmp_path))))


@pytest.mark.asyncio
async def test_task_success(tmp_path: Path) -> None:
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.STDOUT, task_id="x", content="hello\n"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    events = [event async for event in result.events]

    assert [x.type for x in events] == [EventType.STARTED, EventType.STDOUT, EventType.EXITED]

    status = await service.get_status(result.task.task_id, user_id=1)
    assert status is not None
    assert status.status == TaskStatus.SUCCEEDED
    assert status.exit_code == 0


@pytest.mark.asyncio
async def test_task_failed(tmp_path: Path) -> None:
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.STDERR, task_id="x", content="err\n"),
            CLIEvent(type=EventType.FAILED, task_id="x", exit_code=2, error="boom"),
        ]
    )

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
    )

    result = await service.create_and_run(user_id=1, provider="codex", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    status = await service.get_status(result.task.task_id, user_id=1)
    assert status is not None
    assert status.status == TaskStatus.FAILED
    assert status.exit_code == 2
    assert status.failure_reason == "boom"


@pytest.mark.asyncio
async def test_task_timeout(tmp_path: Path) -> None:
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.TIMEOUT, task_id="x", error="timeout"),
        ]
    )

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
    )

    result = await service.create_and_run(user_id=1, provider="gemini", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    status = await service.get_status(result.task.task_id, user_id=1)
    assert status is not None
    assert status.status == TaskStatus.TIMEOUT


@pytest.mark.asyncio
async def test_task_cancel_call(tmp_path: Path) -> None:
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.CANCELED, task_id="x", error="cancel"),
        ]
    )

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))

    canceled = await service.cancel(result.task.task_id, user_id=1)
    assert canceled is True
    assert adapter.cancel_called is True

    _ = [event async for event in result.events]

    status = await service.get_status(result.task.task_id, user_id=1)
    assert status is not None
    assert status.status == TaskStatus.CANCELED


@pytest.mark.asyncio
async def test_output_limit_truncate(tmp_path: Path) -> None:
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.STDOUT, task_id="x", content="12345678901234567890"),
            CLIEvent(type=EventType.STDOUT, task_id="x", content="OVERFLOW"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )

    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=StubFactory(adapter),
        semaphore=asyncio.Semaphore(2),
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    events = [event async for event in result.events]

    assert events[2].content == ""

    status = await service.get_status(result.task.task_id, user_id=1)
    assert status is not None
    assert status.output_chars == 20
    assert status.output_truncated is True


@pytest.mark.asyncio
async def test_tmux_mode_passes_terminal_key(tmp_path: Path) -> None:
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    factory = StubFactory(adapter)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    result = await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))
    _ = [event async for event in result.events]

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert adapter.last_terminal_key == expected
    assert adapter.last_interactive is False
    assert factory._ensured_terminal_key == expected


@pytest.mark.asyncio
async def test_close_terminal_success(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    closed, text = await service.close_terminal(1)
    session = await session_service.get(1)

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert closed is True
    assert text == "终端已关闭"
    assert factory._closed_terminal_key == expected
    assert session is not None
    assert session.terminal_mode is False
    assert session.terminal_id is None
    assert session.claude_chat_active is False


@pytest.mark.asyncio
async def test_open_claude_chat_session_rebuilds_terminal(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=False,
    )

    opened, text = await service.open_claude_chat_session(1)
    session = await session_service.get(1)

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert opened is True
    assert text.startswith("Claude 会话已重建")
    assert "tmux_session" not in text
    assert "terminal_id" not in text
    assert factory._closed_terminal_key == expected
    assert factory._ensured_interactive_terminal_key == expected
    assert factory._ensured_interactive_workdir == str(tmp_path.resolve())
    assert factory._revealed_terminal_key == expected
    assert f"已在桌面打开 Terminal 并附着到 tgcli_{expected}" in text
    assert session is not None
    assert session.provider == "claude_code"
    assert session.terminal_mode is True
    assert session.terminal_id == expected
    assert session.claude_chat_active is True


@pytest.mark.asyncio
async def test_open_claude_chat_session_creates_terminal_without_previous_session(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    opened, text = await service.open_claude_chat_session(1)
    session = await session_service.get(1)

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert opened is True
    assert text.startswith("Claude 会话已开启")
    assert "tmux_session" not in text
    assert "terminal_id" not in text
    assert factory._closed_terminal_key is None
    assert factory._ensured_interactive_terminal_key == expected
    assert factory._ensured_interactive_workdir == str(tmp_path.resolve())
    assert factory._revealed_terminal_key == expected
    assert f"已在桌面打开 Terminal 并附着到 tgcli_{expected}" in text
    assert session is not None
    assert session.claude_chat_active is True


@pytest.mark.asyncio
async def test_create_and_run_claude_uses_claude_provider_in_chat_mode(tmp_path: Path) -> None:
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    await service.open_claude_chat_session(1)

    result = await service.create_and_run(
        user_id=1,
        provider="claude_code",
        prompt="hello",
        workdir=str(tmp_path),
    )
    _ = [event async for event in result.events]

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert result.task.provider == "claude_code"
    assert adapter.last_terminal_key == expected
    assert adapter.last_interactive is True
    assert adapter.last_claude_session_id is None
    assert factory._ensured_interactive_terminal_key == expected


@pytest.mark.asyncio
async def test_create_and_run_fails_when_tmux_ensure_fails(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)

    async def failed_ensure_terminal(*, terminal_key: str, workdir: str) -> tuple[bool, str]:
        return False, "tmux 会话创建失败: no server running"

    factory.ensure_terminal = failed_ensure_terminal
    factory.ensure_claude_interactive_session = failed_ensure_terminal

    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    with pytest.raises(ValueError, match="tmux 会话创建失败"):
        await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))


@pytest.mark.asyncio
async def test_create_and_run_passes_bound_claude_session_id_in_chat_mode(tmp_path: Path) -> None:
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    await service.open_claude_chat_session(1)
    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))

    result = await service.create_and_run(
        user_id=1,
        provider="claude_code",
        prompt="hello again",
        workdir=str(tmp_path),
    )
    _ = [event async for event in result.events]

    assert adapter.last_claude_session_id == "claude-session-1"
    assert result.task.provider == "claude_code"


@pytest.mark.asyncio
async def test_create_and_run_allows_unbound_first_turn_in_chat_mode(tmp_path: Path) -> None:
    adapter = StubAdapter(
        events=[
            CLIEvent(type=EventType.STARTED, task_id="x"),
            CLIEvent(type=EventType.EXITED, task_id="x", exit_code=0),
        ]
    )
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    await service.open_claude_chat_session(1)

    result = await service.create_and_run(
        user_id=1,
        provider="claude_code",
        prompt="first turn",
        workdir=str(tmp_path),
    )
    _ = [event async for event in result.events]

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert result.interactive is True
    assert adapter.last_claude_session_id is None
    assert adapter.last_terminal_key == expected


@pytest.mark.asyncio
async def test_get_or_create_keeps_claude_chat_active_when_not_explicitly_set(tmp_path: Path) -> None:
    session_service = make_file_backed_session_service(tmp_path)
    await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    session = await session_service.get_or_create(
        user_id=1,
        provider="codex",
        workdir=str(tmp_path),
        terminal_mode=False,
    )

    assert session.claude_chat_active is True


@pytest.mark.asyncio
async def test_session_service_terminal_id_changes_with_workdir(tmp_path: Path) -> None:
    session_service = make_file_backed_session_service(tmp_path)
    first = await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path / "one"),
        terminal_mode=True,
        claude_chat_active=True,
    )

    second = await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path / "two"),
        terminal_mode=True,
        claude_chat_active=True,
    )

    assert first.terminal_id != second.terminal_id


@pytest.mark.asyncio
async def test_file_backed_session_service_persists_context(tmp_path: Path) -> None:
    service = make_file_backed_session_service(tmp_path)
    session = await service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))

    reloaded = make_file_backed_session_service(tmp_path)
    restored = await reloaded.get(1)

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert session.session_id
    assert restored is not None
    assert restored.session_id == session.session_id
    assert restored.terminal_id == expected
    assert restored.claude_chat_active is True
    assert restored.claude_session_id == "claude-session-1"
