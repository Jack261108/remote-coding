import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.adapters.storage.file_session_store import FileSessionStore
from app.adapters.storage.memory import MemoryTaskStore
from app.config.settings import Settings
from app.domain.models import CLIEvent, EventType, ExecutionTask, TaskRecord, TaskStatus, utc_now
from app.domain.session_models import ConversationTurn, ParserCheckpoint, PendingPermission, SessionEvent, SessionEventType, SessionPhase, ToolCallRecord, ToolStatus
from app.services.session_service import SessionService
from app.services.session_store import SessionStore
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
        self._interactive_inputs: list[tuple[str, str, str]] = []

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

    async def send_claude_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        self._interactive_inputs.append((terminal_key, workdir, text))
        return True, ""


class DummyHookSocketServer:
    def __init__(self, *, respond_ok: bool = True) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.respond_ok = respond_ok

    async def respond_to_permission(self, *, tool_use_id: str, decision: str, reason: str | None = None) -> bool:
        self.calls.append((tool_use_id, decision, reason))
        return self.respond_ok


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
    assert session.claude_session_id is None


@pytest.mark.asyncio
async def test_open_claude_chat_session_rebuilds_when_previous_terminal_is_missing(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)

    async def missing_close_terminal(terminal_key: str) -> bool:
        factory._closed_terminal_key = terminal_key
        return False

    factory.close_terminal = missing_close_terminal
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

    opened, text = await service.open_claude_chat_session(1)
    session = await session_service.get(1)

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert opened is True
    assert text.startswith("Claude 会话已重建")
    assert factory._closed_terminal_key == expected
    assert factory._ensured_interactive_terminal_key == expected
    assert session is not None
    assert session.terminal_mode is True
    assert session.terminal_id == expected
    assert session.claude_chat_active is True
    assert session.claude_session_id is None


@pytest.mark.asyncio
async def test_open_claude_chat_session_clears_stale_claude_session_without_previous_terminal(tmp_path: Path) -> None:
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

    session = await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=False,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="stale-session")

    opened, text = await service.open_claude_chat_session(1)
    session = await session_service.get(1)

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert opened is True
    assert text.startswith("Claude 会话已开启")
    assert factory._closed_terminal_key is None
    assert session is not None
    assert session.claude_session_id is None
    assert session.terminal_mode is True
    assert session.terminal_id == expected


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
async def test_open_claude_chat_session_switches_to_explicit_workdir(tmp_path: Path) -> None:
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

    original_workdir = str(tmp_path / "one")
    target_workdir = str(tmp_path / "sub dir")
    Path(original_workdir).mkdir(parents=True)
    Path(target_workdir).mkdir(parents=True)

    await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=original_workdir,
        terminal_mode=True,
        claude_chat_active=True,
    )

    opened, text = await service.open_claude_chat_session(1, workdir=target_workdir)
    session = await session_service.get(1)

    old_terminal = expected_terminal_id(user_id=1, workdir=original_workdir)
    new_terminal = expected_terminal_id(user_id=1, workdir=str(Path(target_workdir).resolve()))
    assert opened is True
    assert text.startswith("Claude 会话已重建")
    assert factory._closed_terminal_key == old_terminal
    assert factory._ensured_interactive_terminal_key == new_terminal
    assert factory._ensured_interactive_workdir == str(Path(target_workdir).resolve())
    assert session is not None
    assert session.workdir == str(Path(target_workdir).resolve())
    assert session.terminal_id == new_terminal
    assert session.claude_chat_active is True


@pytest.mark.asyncio
async def test_open_claude_chat_session_rejects_explicit_workdir_outside_allowlist(tmp_path: Path) -> None:
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

    outside = tmp_path.parent / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="workdir 不在 ALLOWED_WORKDIRS 白名单内"):
        await service.open_claude_chat_session(1, workdir=str(outside))


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
    assert result.task.claude_session_id == "claude-session-1"


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
    assert result.task.claude_session_id is None
    assert adapter.last_terminal_key == expected


@pytest.mark.asyncio
async def test_wait_for_structured_session_update_uses_store_cursor(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))
    structured_store.get_or_create(session_id="claude-session-1", workdir=str(tmp_path), claude_session_id="claude-session-1")
    cursor = await service.get_structured_session_cursor(1)

    waiter = asyncio.create_task(service.wait_for_structured_session_update(user_id=1, since_cursor=cursor, timeout_sec=0.2))
    await asyncio.sleep(0)
    structured_store.process(SessionEvent(session_id="claude-session-1", type=SessionEventType.SESSION_STARTED))

    assert await waiter is True
    assert await service.get_structured_session_cursor(1) > cursor


@pytest.mark.asyncio
async def test_structured_session_cursor_ignores_checkpoint_only_updates(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))
    structured_store.get_or_create(session_id="claude-session-1", workdir=str(tmp_path), claude_session_id="claude-session-1")
    cursor = await service.get_structured_session_cursor(1)

    structured_store.save_checkpoint("claude-session-1", ParserCheckpoint(last_offset=9))

    assert await service.get_structured_session_cursor(1) == cursor

    waiter = asyncio.create_task(service.wait_for_structured_session_update(user_id=1, since_cursor=cursor, timeout_sec=0.01))
    assert await waiter is False


@pytest.mark.asyncio
async def test_get_structured_session_for_task_prefers_task_claude_session_id(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    file_store = FileSessionStore(str(tmp_path))
    structured_store = SessionStore(file_store)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    state = structured_store.get_or_create(
        session_id="claude-session-1",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="claude-session-1",
    )
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.turns.append(ConversationTurn(turn_id="turn-1", role="assistant", text="\n你好\n", is_complete=True))
    structured_store._persist(state)

    await service._task_store.add(
        TaskRecord(
            task_id="task-1",
            session_id="session-1",
            user_id=1,
            provider="claude_code",
            prompt="hi",
            workdir=str(tmp_path),
            timeout_sec=10,
            claude_session_id="claude-session-1",
            status=TaskStatus.SUCCEEDED,
        )
    )

    structured = await service.get_structured_session_for_task(task_id="task-1", user_id=1)

    assert structured is not None
    assert structured.session_id == "claude-session-1"


@pytest.mark.asyncio
async def test_get_structured_session_for_task_accepts_uuid_claude_session_id(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    file_store = FileSessionStore(str(tmp_path))
    structured_store = SessionStore(file_store)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    uuid_session_id = "2185ae1c-14e5-4423-8f0d-1b76fcd893d6"
    state = structured_store.get_or_create(
        session_id=uuid_session_id,
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id=uuid_session_id,
    )
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.turns.append(ConversationTurn(turn_id="turn-1", role="assistant", text="\n你好\n", is_complete=True))
    structured_store._persist(state)

    await service._task_store.add(
        TaskRecord(
            task_id="task-uuid",
            session_id="session-1",
            user_id=1,
            provider="claude_code",
            prompt="hi",
            workdir=str(tmp_path),
            timeout_sec=10,
            claude_session_id=uuid_session_id,
            status=TaskStatus.SUCCEEDED,
        )
    )

    structured = await service.get_structured_session_for_task(task_id="task-uuid", user_id=1)

    assert structured is not None
    assert structured.session_id == uuid_session_id


@pytest.mark.asyncio
async def test_get_structured_session_prefers_pending_active_state_over_newer_idle_terminal_state(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    file_store = FileSessionStore(str(tmp_path))
    structured_store = SessionStore(file_store)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    now = utc_now()
    terminal_id = expected_terminal_id(user_id=1, workdir=str(tmp_path))

    pending_state = structured_store.get_or_create(
        session_id="claude-session-pending",
        workdir=str(tmp_path),
        terminal_id=terminal_id,
        claude_session_id="claude-session-pending",
    )
    pending_state.created_at = now - timedelta(minutes=10)
    pending_state.last_activity = now
    pending_state.phase = SessionPhase.WAITING_FOR_APPROVAL
    pending_state.pending_permission = PendingPermission(
        tool_use_id="tool-1",
        tool_name="Bash",
        tool_input={"command": "pwd"},
    )
    structured_store._persist(pending_state)

    idle_state = structured_store.get_or_create(
        session_id="claude-session-idle",
        workdir=str(tmp_path),
        terminal_id=terminal_id,
        claude_session_id="claude-session-idle",
    )
    idle_state.created_at = now
    idle_state.last_activity = now - timedelta(seconds=30)
    idle_state.phase = SessionPhase.WAITING_FOR_INPUT
    structured_store._persist(idle_state)

    structured = await service.get_structured_session(user_id=1)

    assert structured is not None
    assert structured.session_id == "claude-session-pending"
    assert structured.pending_permission is not None
    assert structured.pending_permission.tool_use_id == "tool-1"


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


@pytest.mark.asyncio
async def test_respond_to_pending_permission_uses_resolved_structured_session_not_stale_context(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    hook_socket_server = DummyHookSocketServer()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
        hook_socket_server=hook_socket_server,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="stale-session", workdir=str(tmp_path))

    stale_state = structured_store.get_or_create(
        session_id="stale-session",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="stale-session",
    )
    stale_state.phase = SessionPhase.WAITING_FOR_INPUT
    structured_store._persist(stale_state)

    active_state = structured_store.get_or_create(
        session_id="2185ae1c-14e5-4423-8f0d-1b76fcd893d6",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="2185ae1c-14e5-4423-8f0d-1b76fcd893d6",
    )
    active_state.pending_permission = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    active_state.phase = SessionPhase.WAITING_FOR_APPROVAL
    structured_store._persist(active_state)

    ok, text = await service.respond_to_pending_permission(
        user_id=1,
        decision="allow",
        expected_tool_use_id="tool-1",
    )

    assert ok is True
    assert text == "已批准权限请求: Bash"
    assert hook_socket_server.calls == [("tool-1", "allow", None)]
    updated = structured_store.get("2185ae1c-14e5-4423-8f0d-1b76fcd893d6")
    assert updated is not None
    assert updated.pending_permission is None
    assert updated.phase == SessionPhase.PROCESSING


@pytest.mark.asyncio
async def test_respond_to_pending_permission_prefers_expected_tool_use_id_over_current_session_pointer(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    hook_socket_server = DummyHookSocketServer()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
        hook_socket_server=hook_socket_server,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="current-session", workdir=str(tmp_path))

    current_state = structured_store.get_or_create(
        session_id="current-session",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="current-session",
    )
    current_state.phase = SessionPhase.PROCESSING
    structured_store._persist(current_state)

    pending_state = structured_store.get_or_create(
        session_id="session-with-permission",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="session-with-permission",
    )
    pending_state.pending_permission = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    pending_state.phase = SessionPhase.WAITING_FOR_APPROVAL
    structured_store._persist(pending_state)

    ok, text = await service.respond_to_pending_permission(
        user_id=1,
        decision="allow",
        expected_tool_use_id="tool-1",
    )

    assert ok is True
    assert text == "已批准权限请求: Bash"
    assert hook_socket_server.calls == [("tool-1", "allow", None)]
    updated = structured_store.get("session-with-permission")
    assert updated is not None
    assert updated.pending_permission is None
    assert updated.phase == SessionPhase.PROCESSING


@pytest.mark.asyncio
async def test_respond_to_pending_permission_uses_button_tool_use_id_when_structured_state_missing(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    hook_socket_server = DummyHookSocketServer()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
        hook_socket_server=hook_socket_server,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="current-session", workdir=str(tmp_path))

    ok, text = await service.respond_to_pending_permission(
        user_id=1,
        decision="allow",
        expected_tool_use_id="tool-1",
    )

    assert ok is True
    assert text == "已批准权限请求"
    assert hook_socket_server.calls == [("tool-1", "allow", None)]


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_collects_multi_question_answers_and_sends_to_tmux(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))

    state = structured_store.get_or_create(
        session_id="claude-session-1",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="claude-session-1",
    )
    state.phase = SessionPhase.PROCESSING
    state.tool_calls["tool-ask-1"] = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理范围",
                    "question": "你说的范围我理解为这三块之一，具体按哪种处理？",
                    "options": [
                        {"label": "当前相关改动(推荐)", "description": "只处理相关已改动文件"},
                        {"label": "三个目录全部", "description": "范围非常大"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "提交前置",
                    "question": "按你的 CLAUDE.md，要修改代码前先提交现有改动。现在是否允许我先做这一步？",
                    "options": [
                        {"label": "允许先提交(推荐)", "description": "先提交后继续"},
                        {"label": "暂不允许", "description": "先不改代码"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-1",
        question_index=0,
        option_index=0,
    )

    assert ok is True
    assert text == "已记录选择: 当前相关改动(推荐)"
    assert next_prompt is not None
    assert next_prompt.tool_use_id == "tool-ask-1"
    assert next_prompt.question_index == 1
    assert next_prompt.total_questions == 2
    assert next_prompt.header == "提交前置"
    assert next_prompt.question == "按你的 CLAUDE.md，要修改代码前先提交现有改动。现在是否允许我先做这一步？"

    ok, text, next_prompt = await service.answer_pending_user_question_text(
        user_id=1,
        text="允许先提交(推荐)",
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert factory._interactive_inputs == [
        (
            expected_terminal_id(user_id=1, workdir=str(tmp_path)),
            str(tmp_path),
            "我的选择如下：\n- 处理范围: 当前相关改动(推荐)\n- 提交前置: 允许先提交(推荐)",
        )
    ]


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_rejects_stale_button(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))

    state = structured_store.get_or_create(
        session_id="claude-session-1",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="claude-session-1",
    )
    state.phase = SessionPhase.PROCESSING
    state.tool_calls["tool-ask-2"] = ToolCallRecord(
        tool_use_id="tool-ask-2",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "question": "这两条误写到项目级的记忆，你要我怎么处理？",
                    "options": [{"label": "直接删除", "description": "删除项目级这两条记忆"}],
                    "multiSelect": False,
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-1",
        question_index=0,
        option_index=0,
    )

    assert ok is False
    assert text == "这个选择按钮已经过期，请等待最新的问题"
    assert next_prompt is None
    assert factory._interactive_inputs == []
