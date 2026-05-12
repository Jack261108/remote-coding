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
from app.domain.session_models import ConversationTurn, ParserCheckpoint, PendingPermission, SessionEvent, SessionEventType, SessionPhase, SessionState, ToolCallRecord, ToolStatus
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
        self._user_question_option_actions: list[tuple[str, str, int, bool]] = []
        self._user_question_text_actions: list[tuple[str, str, int, str, bool]] = []
        self._user_question_multi_select_advances: list[tuple[str, str, bool]] = []

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

    async def select_claude_user_question_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        self._user_question_option_actions.append((terminal_key, workdir, option_index, submit_after))
        return True, ""

    async def answer_claude_user_question_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        self._user_question_text_actions.append((terminal_key, workdir, option_count, text, submit_after))
        return True, ""

    async def advance_claude_user_question_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        self._user_question_multi_select_advances.append((terminal_key, workdir, final_question))
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

    task_store = MemoryTaskStore()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=task_store,
        session_service=make_file_backed_session_service(tmp_path),
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    with pytest.raises(ValueError, match="tmux 会话创建失败"):
        await service.create_and_run(user_id=1, provider="claude", prompt="hi", workdir=str(tmp_path))

    tasks = await task_store.iter_all()
    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.FAILED
    assert tasks[0].failure_reason == "tmux 会话创建失败: no server running"
    assert tasks[0].ended_at is not None


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
async def test_get_structured_session_for_task_uses_prompt_turn_after_context_drift(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    file_store = FileSessionStore(str(tmp_path))
    structured_store = SessionStore(file_store)
    task_store = MemoryTaskStore()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=task_store,
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    now = utc_now()
    terminal_id = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await task_store.add(
        TaskRecord(
            task_id="task-1",
            session_id="session-1",
            user_id=1,
            provider="claude_code",
            prompt="hi",
            workdir=str(tmp_path),
            timeout_sec=10,
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
    )

    task_state = structured_store.get_or_create(
        session_id="claude-session-task",
        user_id=1,
        workdir=str(tmp_path),
        terminal_id=terminal_id,
        claude_session_id="claude-session-task",
    )
    task_state.phase = SessionPhase.WAITING_FOR_INPUT
    task_state.turns.extend(
        [
            ConversationTurn(
                turn_id="user-task",
                role="user",
                text="hi",
                is_complete=True,
                started_at=now + timedelta(seconds=1),
                ended_at=now + timedelta(seconds=1),
            ),
            ConversationTurn(
                turn_id="turn-task",
                role="assistant",
                text="\n任务回复\n",
                is_complete=True,
                started_at=now + timedelta(seconds=2),
                ended_at=now + timedelta(seconds=2),
            ),
        ]
    )
    structured_store._persist(task_state)

    drift_state = structured_store.get_or_create(
        session_id="claude-session-other",
        workdir=str(tmp_path),
        terminal_id=terminal_id,
        claude_session_id="claude-session-other",
    )
    drift_state.phase = SessionPhase.WAITING_FOR_APPROVAL
    drift_state.pending_permission = PendingPermission(tool_use_id="tool-other", tool_name="Bash", tool_input={"command": "pwd"})
    structured_store._persist(drift_state)

    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-other", workdir=str(tmp_path))

    structured = await service.get_structured_session_for_task(task_id="task-1", user_id=1)
    updated_task = await task_store.get("task-1")

    assert updated_task is not None
    assert updated_task.claude_session_id == "claude-session-task"
    assert structured is not None
    assert structured.session_id == "claude-session-task"


@pytest.mark.asyncio
async def test_get_structured_session_for_task_prefers_prompt_turn_over_stale_context_session(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    file_store = FileSessionStore(str(tmp_path))
    structured_store = SessionStore(file_store)
    task_store = MemoryTaskStore()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=task_store,
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    now = utc_now()
    terminal_id = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-drift", workdir=str(tmp_path))
    await task_store.add(
        TaskRecord(
            task_id="task-prompt-turn",
            session_id="session-1",
            user_id=1,
            provider="claude_code",
            prompt="明天",
            workdir=str(tmp_path),
            timeout_sec=10,
            claude_session_id="claude-session-drift",
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
    )

    actual_state = structured_store.get_or_create(
        session_id="claude-session-actual",
        user_id=1,
        workdir=str(tmp_path),
        terminal_id=terminal_id,
        claude_session_id="claude-session-actual",
    )
    actual_state.phase = SessionPhase.WAITING_FOR_INPUT
    actual_state.turns.extend(
        [
            ConversationTurn(
                turn_id="user-actual",
                role="user",
                text="明天",
                is_complete=True,
                started_at=now + timedelta(seconds=1),
                ended_at=now + timedelta(seconds=1),
            ),
            ConversationTurn(
                turn_id="resp-actual",
                role="assistant",
                text="\n任务对应回复\n",
                is_complete=True,
                started_at=now + timedelta(seconds=2),
                ended_at=now + timedelta(seconds=2),
            ),
        ]
    )
    structured_store._persist(actual_state)

    drift_state = structured_store.get_or_create(
        session_id="claude-session-drift",
        user_id=1,
        workdir=str(tmp_path),
        terminal_id=terminal_id,
        claude_session_id="claude-session-drift",
    )
    drift_state.phase = SessionPhase.WAITING_FOR_APPROVAL
    drift_state.pending_permission = PendingPermission(tool_use_id="tool-drift", tool_name="Bash", tool_input={"command": "pwd"})
    drift_state.turns.append(
        ConversationTurn(
            turn_id="resp-drift",
            role="assistant",
            text="\n漂移会话回复\n",
            is_complete=True,
            started_at=now + timedelta(seconds=3),
            ended_at=now + timedelta(seconds=3),
        )
    )
    structured_store._persist(drift_state)

    structured = await service.get_structured_session_for_task(task_id="task-prompt-turn", user_id=1)
    updated_task = await task_store.get("task-prompt-turn")

    assert structured is not None
    assert structured.session_id == "claude-session-actual"
    assert updated_task is not None
    assert updated_task.claude_session_id == "claude-session-actual"


@pytest.mark.asyncio
async def test_get_structured_session_for_task_keeps_final_task_bound_when_later_same_prompt_exists(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    file_store = FileSessionStore(str(tmp_path))
    structured_store = SessionStore(file_store)
    task_store = MemoryTaskStore()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=task_store,
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )

    now = utc_now()
    terminal_id = expected_terminal_id(user_id=1, workdir=str(tmp_path))
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await task_store.add(
        TaskRecord(
            task_id="task-final",
            session_id="session-1",
            user_id=1,
            provider="claude_code",
            prompt="hi",
            workdir=str(tmp_path),
            timeout_sec=10,
            claude_session_id="claude-session-original",
            status=TaskStatus.SUCCEEDED,
            created_at=now,
            started_at=now,
            ended_at=now + timedelta(seconds=3),
        )
    )

    original_state = structured_store.get_or_create(
        session_id="claude-session-original",
        user_id=1,
        workdir=str(tmp_path),
        terminal_id=terminal_id,
        claude_session_id="claude-session-original",
    )
    original_state.phase = SessionPhase.WAITING_FOR_INPUT
    original_state.turns.extend(
        [
            ConversationTurn(turn_id="user-original", role="user", text="hi", is_complete=True, started_at=now + timedelta(seconds=1)),
            ConversationTurn(turn_id="resp-original", role="assistant", text="\n原任务回复\n", is_complete=True, started_at=now + timedelta(seconds=2)),
        ]
    )
    structured_store._persist(original_state)

    later_state = structured_store.get_or_create(
        session_id="claude-session-later",
        user_id=1,
        workdir=str(tmp_path),
        terminal_id=terminal_id,
        claude_session_id="claude-session-later",
    )
    later_state.phase = SessionPhase.WAITING_FOR_INPUT
    later_state.turns.extend(
        [
            ConversationTurn(turn_id="user-later", role="user", text="hi", is_complete=True, started_at=now + timedelta(seconds=4)),
            ConversationTurn(turn_id="resp-later", role="assistant", text="\n后续任务回复\n", is_complete=True, started_at=now + timedelta(seconds=5)),
        ]
    )
    structured_store._persist(later_state)

    structured = await service.get_structured_session_for_task(task_id="task-final", user_id=1)
    updated_task = await task_store.get("task-final")

    assert structured is not None
    assert structured.session_id == "claude-session-original"
    assert updated_task is not None
    assert updated_task.claude_session_id == "claude-session-original"


@pytest.mark.asyncio
async def test_bind_claude_session_does_not_bulk_rebind_unmatched_tasks(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    task_store = MemoryTaskStore()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=task_store,
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )

    now = utc_now()
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await task_store.add(
        TaskRecord(
            task_id="task-a",
            session_id="session-a",
            user_id=1,
            provider="claude_code",
            prompt="first",
            workdir=str(tmp_path),
            timeout_sec=10,
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
    )
    await task_store.add(
        TaskRecord(
            task_id="task-b",
            session_id="session-b",
            user_id=1,
            provider="claude_code",
            prompt="second",
            workdir=str(tmp_path),
            timeout_sec=10,
            status=TaskStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
    )

    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-new", workdir=str(tmp_path))

    task_a = await task_store.get("task-a")
    task_b = await task_store.get("task-b")

    assert task_a is not None
    assert task_a.claude_session_id is None
    assert task_b is not None
    assert task_b.claude_session_id is None


@pytest.mark.asyncio
async def test_get_structured_session_for_task_rejects_stale_task_session_from_other_workdir(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    file_store = FileSessionStore(str(tmp_path))
    structured_store = SessionStore(file_store)
    task_store = MemoryTaskStore()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=task_store,
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
        structured_session_store=structured_store,
    )
    old_workdir = str(tmp_path / "old")
    new_workdir = str(tmp_path / "new")
    terminal_id = expected_terminal_id(user_id=1, workdir=new_workdir)

    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=new_workdir,
        terminal_mode=True,
        claude_chat_active=True,
    )
    await task_store.add(
        TaskRecord(
            task_id="task-stale",
            session_id="session-1",
            user_id=1,
            provider="claude_code",
            prompt="hi",
            workdir=new_workdir,
            timeout_sec=10,
            claude_session_id="claude-session-old",
            status=TaskStatus.RUNNING,
        )
    )

    old_state = structured_store.get_or_create(
        session_id="claude-session-old",
        workdir=old_workdir,
        terminal_id=expected_terminal_id(user_id=1, workdir=old_workdir),
        claude_session_id="claude-session-old",
    )
    old_state.phase = SessionPhase.WAITING_FOR_INPUT
    old_state.turns.append(ConversationTurn(turn_id="turn-old", role="assistant", text="\n旧回复\n", is_complete=True))
    structured_store._persist(old_state)

    new_state = structured_store.get_or_create(
        session_id="claude-session-new",
        workdir=new_workdir,
        terminal_id=terminal_id,
        claude_session_id="claude-session-new",
    )
    new_state.phase = SessionPhase.WAITING_FOR_INPUT
    new_state.turns.append(ConversationTurn(turn_id="turn-new", role="assistant", text="\n新回复\n", is_complete=True))
    structured_store._persist(new_state)

    structured_before_bind = await service.get_structured_session_for_task(task_id="task-stale", user_id=1)
    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-new", workdir=new_workdir)
    updated_task = await task_store.get("task-stale")
    structured_after_bind = await service.get_structured_session_for_task(task_id="task-stale", user_id=1)

    assert structured_before_bind is not None
    assert structured_before_bind.session_id == "claude-session-new"
    assert updated_task is not None
    assert updated_task.claude_session_id == "claude-session-old"
    assert structured_after_bind is not None
    assert structured_after_bind.session_id == "claude-session-new"


@pytest.mark.asyncio
async def test_get_structured_session_for_task_rejects_stale_fallback_session_from_other_workdir(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    task_store = MemoryTaskStore()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=task_store,
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(2),
    )
    old_workdir = str(tmp_path / "old")
    new_workdir = str(tmp_path / "new")
    stale_state = SessionState(
        session_id="claude-session-old",
        workdir=old_workdir,
        claude_session_id="claude-session-old",
        phase=SessionPhase.WAITING_FOR_INPUT,
    )
    factory.get_claude_session_state = lambda session_id: stale_state
    await task_store.add(
        TaskRecord(
            task_id="task-fallback-stale",
            session_id="session-1",
            user_id=1,
            provider="claude_code",
            prompt="hi",
            workdir=new_workdir,
            timeout_sec=10,
            claude_session_id="claude-session-old",
            status=TaskStatus.RUNNING,
        )
    )

    structured = await service.get_structured_session_for_task(task_id="task-fallback-stale", user_id=1)

    assert structured is None


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
async def test_respond_to_pending_permission_keeps_state_when_socket_response_fails(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    hook_socket_server = DummyHookSocketServer(respond_ok=False)
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
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-pending", workdir=str(tmp_path))

    state = structured_store.get_or_create(
        session_id="claude-session-pending",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="claude-session-pending",
    )
    state.pending_permission = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    structured_store._persist(state)

    ok, text = await service.respond_to_pending_permission(
        user_id=1,
        decision="allow",
        expected_tool_use_id="tool-1",
    )

    assert ok is False
    assert text == "待处理权限请求已失效，请等待 Claude 重新发起"
    assert hook_socket_server.calls == [("tool-1", "allow", None)]
    updated = structured_store.get("claude-session-pending")
    assert updated is not None
    assert updated.pending_permission is not None
    assert updated.pending_permission.tool_use_id == "tool-1"
    assert updated.phase == SessionPhase.WAITING_FOR_APPROVAL


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
async def test_respond_to_pending_permission_rejects_button_tool_use_id_when_structured_state_missing(tmp_path: Path) -> None:
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

    assert ok is False
    assert text == "这个权限按钮已经过期，请等待最新的权限请求"
    assert hook_socket_server.calls == []


@pytest.mark.asyncio
async def test_respond_to_pending_permission_serializes_same_tool_use_id_callbacks(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))

    class BlockingHookSocketServer(DummyHookSocketServer):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def respond_to_permission(self, *, tool_use_id: str, decision: str, reason: str | None = None) -> bool:
            self.calls.append((tool_use_id, decision, reason))
            if len(self.calls) == 1:
                self.entered.set()
                await self.release.wait()
            return True

    hook_socket_server = BlockingHookSocketServer()
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
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-permission", workdir=str(tmp_path))

    state = structured_store.get_or_create(
        session_id="claude-session-permission",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="claude-session-permission",
    )
    state.pending_permission = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    structured_store._persist(state)

    first = asyncio.create_task(
        service.respond_to_pending_permission(
            user_id=1,
            decision="allow",
            expected_tool_use_id="tool-1",
        )
    )
    await hook_socket_server.entered.wait()
    second = asyncio.create_task(
        service.respond_to_pending_permission(
            user_id=1,
            decision="deny",
            expected_tool_use_id="tool-1",
        )
    )
    await asyncio.sleep(0.03)
    calls_during_block = list(hook_socket_server.calls)

    hook_socket_server.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert calls_during_block == [("tool-1", "allow", None)]
    assert first_result == (True, "已批准权限请求: Bash")
    assert second_result == (False, "这个权限按钮已经过期，请等待最新的权限请求")
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
    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 0, False),
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 0, True),
    ]
    assert factory._user_question_text_actions == []


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
    assert factory._user_question_option_actions == []


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_rejects_older_active_tool_button(tmp_path: Path) -> None:
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
    state.tool_calls["tool-ask-old"] = ToolCallRecord(
        tool_use_id="tool-ask-old",
        name="AskUserQuestion",
        input={"questions": [{"question": "旧问题", "options": [{"label": "旧选项"}], "multiSelect": False}]},
        status=ToolStatus.RUNNING,
        started_at=utc_now() - timedelta(minutes=1),
    )
    state.tool_calls["tool-ask-new"] = ToolCallRecord(
        tool_use_id="tool-ask-new",
        name="AskUserQuestion",
        input={"questions": [{"question": "新问题", "options": [{"label": "新选项"}], "multiSelect": False}]},
        status=ToolStatus.RUNNING,
        started_at=utc_now(),
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-old",
        question_index=0,
        option_index=0,
    )

    assert ok is False
    assert text == "这个选择按钮已经过期，请等待最新的问题"
    assert next_prompt is None
    assert factory._user_question_option_actions == []


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_rejects_already_answered_question_button(tmp_path: Path) -> None:
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
    state.tool_calls["tool-ask-old-button"] = ToolCallRecord(
        tool_use_id="tool-ask-old-button",
        name="AskUserQuestion",
        input={
            "questions": [
                {"question": "第一题", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False},
                {"question": "第二题", "options": [{"label": "C"}, {"label": "D"}], "multiSelect": False},
            ]
        },
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-old-button",
        question_index=0,
        option_index=0,
    )

    assert ok is True
    assert text == "已记录选择: A"
    assert next_prompt is not None
    assert next_prompt.question_index == 1

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-old-button",
        question_index=0,
        option_index=1,
    )

    assert ok is False
    assert text == "这个选择按钮已经过期，请等待最新的问题"
    assert next_prompt is None
    assert factory._user_question_option_actions == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 0, False)
    ]


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_serializes_same_user_and_tool_callbacks(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    entered = asyncio.Event()
    release = asyncio.Event()
    option_actions: list[tuple[str, str, int, bool]] = []

    async def blocking_select(*, terminal_key: str, workdir: str, option_index: int, submit_after: bool = False) -> tuple[bool, str]:
        option_actions.append((terminal_key, workdir, option_index, submit_after))
        if len(option_actions) == 1:
            entered.set()
            await release.wait()
        return True, ""

    factory.select_claude_user_question_option = blocking_select
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
    state.tool_calls["tool-ask-concurrent"] = ToolCallRecord(
        tool_use_id="tool-ask-concurrent",
        name="AskUserQuestion",
        input={
            "questions": [
                {"question": "第一题", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False},
                {"question": "第二题", "options": [{"label": "C"}, {"label": "D"}], "multiSelect": False},
            ]
        },
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(state)

    first = asyncio.create_task(
        service.answer_pending_user_question_option(
            user_id=1,
            tool_use_id="tool-ask-concurrent",
            question_index=0,
            option_index=0,
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        service.answer_pending_user_question_option(
            user_id=1,
            tool_use_id="tool-ask-concurrent",
            question_index=0,
            option_index=1,
        )
    )
    await asyncio.sleep(0.03)

    assert option_actions == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 0, False)
    ]

    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result[0] is True
    assert first_result[2] is not None
    assert second_result == (False, "这个选择按钮已经过期，请等待最新的问题", None)
    assert option_actions == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 0, False)
    ]


@pytest.mark.asyncio
async def test_answer_pending_user_question_text_serializes_with_option_callback(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    entered = asyncio.Event()
    release = asyncio.Event()
    option_actions: list[tuple[str, str, int, bool]] = []

    async def blocking_select(*, terminal_key: str, workdir: str, option_index: int, submit_after: bool = False) -> tuple[bool, str]:
        option_actions.append((terminal_key, workdir, option_index, submit_after))
        entered.set()
        await release.wait()
        return True, ""

    factory.select_claude_user_question_option = blocking_select
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
    state.tool_calls["tool-ask-text-race"] = ToolCallRecord(
        tool_use_id="tool-ask-text-race",
        name="AskUserQuestion",
        input={"questions": [{"question": "第一题", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False}]},
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(state)

    first = asyncio.create_task(
        service.answer_pending_user_question_option(
            user_id=1,
            tool_use_id="tool-ask-text-race",
            question_index=0,
            option_index=0,
        )
    )
    await entered.wait()
    second = asyncio.create_task(service.answer_pending_user_question_text(user_id=1, text="手动输入"))
    await asyncio.sleep(0.03)
    text_actions_during_block = list(factory._user_question_text_actions)
    interactive_inputs_during_block = list(factory._interactive_inputs)

    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert text_actions_during_block == []
    assert interactive_inputs_during_block == []
    assert first_result == (True, "已提交你的选择，Claude 继续执行中", None)
    assert second_result == (False, "当前没有待处理的选择题", None)


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_falls_back_to_text_transport_when_terminal_not_tui(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)

    async def not_tui(*, terminal_key: str, workdir: str, option_index: int, submit_after: bool = False) -> tuple[bool, str]:
        return False, "当前问题不是 Claude 选择框界面，将回退为文本回答"

    factory.select_claude_user_question_option = not_tui
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
    state.tool_calls["tool-ask-text"] = ToolCallRecord(
        tool_use_id="tool-ask-text",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "出发日期",
                    "question": "你想查哪一天出发？",
                    "options": [
                        {"label": "今天", "description": "查询今天出发的车票"},
                        {"label": "明天", "description": "查询明天出发的车票"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "到达站",
                    "question": "你希望到哪个站？",
                    "options": [
                        {"label": "西安站", "description": "只查询西安站"},
                        {"label": "都查", "description": "同时查询西安相关车站"},
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
        tool_use_id="tool-ask-text",
        question_index=0,
        option_index=1,
    )

    assert ok is True
    assert text == "已记录选择: 明天"
    assert next_prompt is not None

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-text",
        question_index=1,
        option_index=1,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert factory._interactive_inputs == [
        (
            expected_terminal_id(user_id=1, workdir=str(tmp_path)),
            str(tmp_path),
            "我的选择如下：\n- 出发日期: 明天\n- 到达站: 都查",
        )
    ]
    assert factory._user_question_option_actions == []


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_falls_back_when_factory_lacks_terminal_option_method(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])

    class LegacyFactory:
        def __init__(self) -> None:
            self._interactive_inputs: list[tuple[str, str, str]] = []

        async def send_claude_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
            self._interactive_inputs.append((terminal_key, workdir, text))
            return True, ""

    factory = LegacyFactory()
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
    state.tool_calls["tool-ask-legacy"] = ToolCallRecord(
        tool_use_id="tool-ask-legacy",
        name="AskUserQuestion",
        input={"questions": [{"question": "选择", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False}]},
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-legacy",
        question_index=0,
        option_index=1,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert factory._interactive_inputs == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), "B")
    ]


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_falls_back_on_short_tui_fallback_prefix(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)

    async def not_tui_with_detail(*, terminal_key: str, workdir: str, option_index: int, submit_after: bool = False) -> tuple[bool, str]:
        return False, "当前问题不是 Claude 选择框界面：capture-pane 为空"

    factory.select_claude_user_question_option = not_tui_with_detail
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
    state.tool_calls["tool-ask-prefix"] = ToolCallRecord(
        tool_use_id="tool-ask-prefix",
        name="AskUserQuestion",
        input={"questions": [{"question": "选择", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False}]},
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-prefix",
        question_index=0,
        option_index=1,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert factory._interactive_inputs == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), "B")
    ]


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_uses_pending_permission_ask_user_question(tmp_path: Path) -> None:
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
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))

    state = structured_store.get_or_create(
        session_id="claude-session-1",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="claude-session-1",
    )
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    state.pending_permission = PendingPermission(
        tool_use_id="tool-ask-pending",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {
                    "header": "出发日期",
                    "question": "你想查哪一天出发？",
                    "options": [
                        {"label": "今天", "description": "查询今天从郑州到西安的车票"},
                        {"label": "明天", "description": "查询明天从郑州到西安的车票"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "出发站",
                    "question": "你希望从哪个站出发？",
                    "options": [
                        {"label": "郑州站", "description": "只查询郑州站"},
                        {"label": "都查", "description": "同时查询郑州相关车站"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-pending",
        question_index=0,
        option_index=1,
    )

    assert ok is True
    assert text == "已记录选择: 明天"
    assert next_prompt is not None
    assert next_prompt.question_index == 1
    assert hook_socket_server.calls == []
    updated = structured_store.get("claude-session-1")
    assert updated is not None
    assert updated.structured_user_question_key == "tool-ask-pending:1"

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-pending",
        question_index=1,
        option_index=1,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 1, False),
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 1, True),
    ]
    assert hook_socket_server.calls == [("tool-ask-pending", "allow", None)]
    updated = structured_store.get("claude-session-1")
    assert updated is not None
    assert updated.pending_permission is None
    assert updated.phase == SessionPhase.PROCESSING


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_keeps_pending_when_hook_response_fails(tmp_path: Path) -> None:
    adapter = StubAdapter(events=[])
    factory = StubFactory(adapter)
    session_service = make_file_backed_session_service(tmp_path)
    structured_store = SessionStore(FileSessionStore(str(tmp_path)))
    hook_socket_server = DummyHookSocketServer(respond_ok=False)
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
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))

    state = structured_store.get_or_create(
        session_id="claude-session-1",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="claude-session-1",
    )
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    state.pending_permission = PendingPermission(
        tool_use_id="tool-ask-pending",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {
                    "question": "你想查哪一天出发？",
                    "options": [
                        {"label": "今天", "description": "查询今天"},
                        {"label": "明天", "description": "查询明天"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-pending",
        question_index=0,
        option_index=1,
    )

    assert ok is False
    assert text == "待处理权限请求已失效，请等待 Claude 重新发起"
    assert next_prompt is None
    assert hook_socket_server.calls == [("tool-ask-pending", "allow", None)]
    updated = structured_store.get("claude-session-1")
    assert updated is not None
    assert updated.pending_permission is not None
    assert updated.pending_permission.tool_use_id == "tool-ask-pending"
    assert updated.phase == SessionPhase.WAITING_FOR_APPROVAL
    prompts = await service.get_pending_user_questions(user_id=1)
    assert len(prompts) == 1
    assert prompts[0].tool_use_id == "tool-ask-pending"


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_promotes_next_real_permission_after_final_answer(tmp_path: Path) -> None:
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
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))

    state = structured_store.get_or_create(
        session_id="claude-session-1",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        claude_session_id="claude-session-1",
    )
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    state.pending_permission = PendingPermission(
        tool_use_id="tool-ask-pending",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {
                    "header": "出发日期",
                    "question": "你想查哪一天出发？",
                    "options": [
                        {"label": "今天", "description": "查询今天从郑州到西安的车票"},
                        {"label": "明天", "description": "查询明天从郑州到西安的车票"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
    )
    state.tool_calls["tool-bash-1"] = ToolCallRecord(
        tool_use_id="tool-bash-1",
        name="Bash",
        input={"command": "pwd"},
        status=ToolStatus.WAITING_FOR_APPROVAL,
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-pending",
        question_index=0,
        option_index=1,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert hook_socket_server.calls == [("tool-ask-pending", "allow", None)]
    updated = structured_store.get("claude-session-1")
    assert updated is not None
    assert updated.phase == SessionPhase.WAITING_FOR_APPROVAL
    assert updated.pending_permission is not None
    assert updated.pending_permission.tool_use_id == "tool-bash-1"
    assert updated.pending_permission.tool_name == "Bash"


@pytest.mark.asyncio
async def test_acknowledge_structured_user_question_marks_target_state_by_question_key_when_session_drifts(tmp_path: Path) -> None:
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
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-current", workdir=str(tmp_path))

    current_state = structured_store.get_or_create(
        session_id="claude-session-current",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        user_id=1,
        claude_session_id="claude-session-current",
    )
    current_state.phase = SessionPhase.WAITING_FOR_INPUT
    structured_store._persist(current_state)

    ask_state = structured_store.get_or_create(
        session_id="claude-session-ask",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        user_id=1,
        claude_session_id="claude-session-ask",
    )
    ask_state.phase = SessionPhase.PROCESSING
    ask_state.tool_calls["tool-ask-1"] = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {"question": "第一题", "options": [{"label": "A"}], "multiSelect": False},
                {"question": "第二题", "options": [{"label": "B"}], "multiSelect": False},
            ]
        },
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(ask_state)

    await service.acknowledge_structured_user_question(user_id=1, question_key="tool-ask-1:1")

    updated_current = structured_store.get("claude-session-current")
    updated_ask = structured_store.get("claude-session-ask")
    assert updated_current is not None
    assert updated_current.structured_user_question_key is None
    assert updated_ask is not None
    assert updated_ask.structured_user_question_key == "tool-ask-1:1"


@pytest.mark.asyncio
async def test_acknowledge_structured_user_question_ignores_cross_user_question_key(tmp_path: Path) -> None:
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
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-ask", workdir=str(tmp_path))
    await session_service.switch(
        user_id=2,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=2, claude_session_id="claude-session-other", workdir=str(tmp_path))

    ask_state = structured_store.get_or_create(
        session_id="claude-session-ask",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        user_id=1,
        claude_session_id="claude-session-ask",
    )
    ask_state.phase = SessionPhase.PROCESSING
    ask_state.tool_calls["tool-ask-1"] = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={"questions": [{"question": "第一题", "options": [{"label": "A"}], "multiSelect": False}]},
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(ask_state)

    await service.acknowledge_structured_user_question(user_id=2, question_key="tool-ask-1:0")

    updated_ask = structured_store.get("claude-session-ask")
    assert updated_ask is not None
    assert updated_ask.structured_user_question_key is None


@pytest.mark.asyncio
async def test_get_structured_user_question_cursor_uses_draft_target_when_current_session_drifts(tmp_path: Path) -> None:
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
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session-current", workdir=str(tmp_path))

    current_state = structured_store.get_or_create(
        session_id="claude-session-current",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        user_id=1,
        claude_session_id="claude-session-current",
    )
    current_state.phase = SessionPhase.WAITING_FOR_INPUT
    structured_store._persist(current_state)

    ask_state = structured_store.get_or_create(
        session_id="claude-session-ask",
        workdir=str(tmp_path),
        terminal_id=expected_terminal_id(user_id=1, workdir=str(tmp_path)),
        user_id=1,
        claude_session_id="claude-session-ask",
    )
    ask_state.phase = SessionPhase.PROCESSING
    ask_state.tool_calls["tool-ask-1"] = ToolCallRecord(
        tool_use_id="tool-ask-1",
        name="AskUserQuestion",
        input={
            "questions": [
                {"question": "第一题", "options": [{"label": "A"}], "multiSelect": False},
                {"question": "第二题", "options": [{"label": "B"}], "multiSelect": False},
            ]
        },
        status=ToolStatus.RUNNING,
    )
    ask_state.structured_user_question_key = "tool-ask-1:1"
    structured_store._persist(ask_state)

    prompts = service._extract_user_question_prompts_for_tool_use_id(ask_state, tool_use_id="tool-ask-1")
    service._ensure_user_question_draft(user_id=1, prompts=prompts)

    cursor = await service.get_structured_user_question_cursor(user_id=1)

    assert cursor == "tool-ask-1:1"


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_uses_waiting_for_approval_tool_when_pending_permission_missing(tmp_path: Path) -> None:
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
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    state.pending_permission = None
    state.tool_calls["tool-ask-waiting"] = ToolCallRecord(
        tool_use_id="tool-ask-waiting",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "出发日期",
                    "question": "你想查哪一天出发？",
                    "options": [
                        {"label": "今天", "description": "查询今天从郑州到西安的车票"},
                        {"label": "明天", "description": "查询明天从郑州到西安的车票"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "出发站",
                    "question": "你希望从哪个站出发？",
                    "options": [
                        {"label": "郑州站", "description": "只查询郑州站"},
                        {"label": "都查", "description": "同时查询郑州相关车站"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
        status=ToolStatus.WAITING_FOR_APPROVAL,
    )
    structured_store._persist(state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-waiting",
        question_index=0,
        option_index=1,
    )

    assert ok is True
    assert text == "已记录选择: 明天"
    assert next_prompt is not None
    assert next_prompt.question_index == 1
    updated = structured_store.get("claude-session-1")
    assert updated is not None
    assert updated.structured_user_question_key == "tool-ask-waiting:1"
    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 1, False)
    ]


@pytest.mark.asyncio
async def test_submit_pending_user_question_multi_select_collects_checked_options_and_sends_to_tmux(tmp_path: Path) -> None:
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
    state.tool_calls["tool-ask-multi"] = ToolCallRecord(
        tool_use_id="tool-ask-multi",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理方式",
                    "question": "这次要保留哪些动作？",
                    "options": [
                        {"label": "保留日志", "description": "继续输出调试日志"},
                        {"label": "保留测试", "description": "继续保留回归测试"},
                        {"label": "保留重启", "description": "继续自动重启 bot"},
                    ],
                    "multiSelect": True,
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(state)

    ok, text, prompt, selected = await service.toggle_pending_user_question_multi_select_option(
        user_id=1,
        tool_use_id="tool-ask-multi",
        question_index=0,
        option_index=0,
    )

    assert ok is True
    assert text == "已选择: 保留日志"
    assert prompt is not None
    assert prompt.multi_select is True
    assert selected == frozenset({0})

    ok, text, prompt, selected = await service.toggle_pending_user_question_multi_select_option(
        user_id=1,
        tool_use_id="tool-ask-multi",
        question_index=0,
        option_index=2,
    )

    assert ok is True
    assert text == "已选择: 保留重启"
    assert prompt is not None
    assert selected == frozenset({0, 2})

    ok, text, next_prompt = await service.submit_pending_user_question_multi_select(
        user_id=1,
        tool_use_id="tool-ask-multi",
        question_index=0,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 0, False),
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), 2, False),
    ]
    assert factory._user_question_multi_select_advances == [
        (expected_terminal_id(user_id=1, workdir=str(tmp_path)), str(tmp_path), True)
    ]


@pytest.mark.asyncio
async def test_answer_pending_user_question_option_uses_button_tool_use_id_when_session_context_is_stale(tmp_path: Path) -> None:
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

    active_workdir = str(tmp_path / "active")
    stale_workdir = str(tmp_path / "stale")
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=stale_workdir,
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="stale-session", workdir=stale_workdir)

    stale_state = structured_store.get_or_create(
        session_id="stale-session",
        workdir=stale_workdir,
        terminal_id=expected_terminal_id(user_id=1, workdir=stale_workdir),
        claude_session_id="stale-session",
    )
    stale_state.phase = SessionPhase.WAITING_FOR_INPUT
    structured_store._persist(stale_state)

    active_state = structured_store.get_or_create(
        session_id="claude-session-ask",
        workdir=active_workdir,
        terminal_id=expected_terminal_id(user_id=1, workdir=active_workdir),
        user_id=1,
        claude_session_id="claude-session-ask",
    )
    active_state.phase = SessionPhase.PROCESSING
    active_state.tool_calls["tool-ask-1"] = ToolCallRecord(
        tool_use_id="tool-ask-1",
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
    structured_store._persist(active_state)

    ok, text, next_prompt = await service.answer_pending_user_question_option(
        user_id=1,
        tool_use_id="tool-ask-1",
        question_index=0,
        option_index=0,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == [
        (expected_terminal_id(user_id=1, workdir=active_workdir), active_workdir, 0, True)
    ]


@pytest.mark.asyncio
async def test_submit_pending_user_question_multi_select_uses_button_tool_use_id_when_session_context_is_stale(tmp_path: Path) -> None:
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

    active_workdir = str(tmp_path / "active-multi")
    stale_workdir = str(tmp_path / "stale-multi")
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=stale_workdir,
        terminal_mode=True,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="stale-session", workdir=stale_workdir)

    stale_state = structured_store.get_or_create(
        session_id="stale-session",
        workdir=stale_workdir,
        terminal_id=expected_terminal_id(user_id=1, workdir=stale_workdir),
        claude_session_id="stale-session",
    )
    stale_state.phase = SessionPhase.WAITING_FOR_INPUT
    structured_store._persist(stale_state)

    active_state = structured_store.get_or_create(
        session_id="claude-session-ask-multi",
        workdir=active_workdir,
        terminal_id=expected_terminal_id(user_id=1, workdir=active_workdir),
        user_id=1,
        claude_session_id="claude-session-ask-multi",
    )
    active_state.phase = SessionPhase.PROCESSING
    active_state.tool_calls["tool-ask-multi"] = ToolCallRecord(
        tool_use_id="tool-ask-multi",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理方式",
                    "question": "这次要保留哪些动作？",
                    "options": [
                        {"label": "保留日志", "description": "继续输出调试日志"},
                        {"label": "保留测试", "description": "继续保留回归测试"},
                    ],
                    "multiSelect": True,
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    structured_store._persist(active_state)

    ok, text, prompt, selected = await service.toggle_pending_user_question_multi_select_option(
        user_id=1,
        tool_use_id="tool-ask-multi",
        question_index=0,
        option_index=0,
    )

    assert ok is True
    assert text == "已选择: 保留日志"
    assert prompt is not None
    assert selected == frozenset({0})

    ok, text, next_prompt = await service.submit_pending_user_question_multi_select(
        user_id=1,
        tool_use_id="tool-ask-multi",
        question_index=0,
    )

    assert ok is True
    assert text == "已提交你的选择，Claude 继续执行中"
    assert next_prompt is None
    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == [
        (expected_terminal_id(user_id=1, workdir=active_workdir), active_workdir, 0, False)
    ]
    assert factory._user_question_multi_select_advances == [
        (expected_terminal_id(user_id=1, workdir=active_workdir), active_workdir, True)
    ]
