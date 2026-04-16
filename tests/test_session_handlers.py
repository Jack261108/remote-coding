import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.process.tmux_runner import TmuxRunner
from app.adapters.storage.memory import MemorySessionStore, MemoryTaskStore
from app.bot.handlers.command_permission import register_permission_handlers
from app.bot.handlers.command_session import register_session_handler
from app.bot.handlers.command_status import register_status_handler
from app.bot.router import create_router
from app.config.settings import Settings
from app.domain.models import TaskRecord, TaskStatus
from app.domain.session_models import ConversationTurn, SessionEvent, SessionEventType, SessionPhase, PendingPermission
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from tests.test_task_service import StubAdapter, StubFactory


class DummyHookSocketServer:
    def __init__(self, *, respond_ok: bool = True) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.respond_ok = respond_ok

    async def respond_to_permission(self, *, tool_use_id: str, decision: str, reason: str | None = None) -> bool:
        self.calls.append((tool_use_id, decision, reason))
        return self.respond_ok


class DummyMessage:
    def __init__(self, text: str, user_id: int = 1) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


class DummyRouter:
    def __init__(self) -> None:
        self.handlers = []

    def message(self, *args, **kwargs):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator


def make_settings(tmp_path, *, claude_tmux_mode: bool = True) -> Settings:
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
        }
    )


@pytest.mark.asyncio
async def test_session_handler_renders_structured_snapshot(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=SessionService(MemorySessionStore()),
        cli_factory=factory,
        semaphore=asyncio.Semaphore(1),
        structured_session_store=tmux_runner._session_store,
    )
    session_service = service._session_service
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))
    state = tmux_runner._session_store.get_or_create(session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f")
    tmux_runner._session_store.process(SessionEvent(session_id="claude-session-1", type=SessionEventType.SESSION_STARTED))
    tmux_runner._session_store.process(
        SessionEvent(session_id="claude-session-1", type=SessionEventType.TURN_STARTED, payload={"turn_id": "turn-1", "role": "assistant"})
    )
    state.turns[-1].text = "\n你好\n"
    state.turns[-1].is_complete = True
    state = tmux_runner._session_store.process(
        SessionEvent(session_id="claude-session-1", type=SessionEventType.TURN_COMPLETED, payload={"turn_id": "turn-1"})
    )
    state.phase = SessionPhase.WAITING_FOR_INPUT
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    register_session_handler(router, task_service=service, session_service=session_service)
    handler = router.handlers[0]
    message = DummyMessage("/session")

    await handler(message)

    assert message.answers
    assert "structured_session:" in message.answers[0]
    assert "phase: waiting_for_input" in message.answers[0]
    assert "last_reply: 你好" in message.answers[0]
    assert "terminal_mode" not in message.answers[0]
    assert "terminal_id" not in message.answers[0]


@pytest.mark.asyncio
async def test_status_handler_renders_structured_snapshot(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=SessionService(MemorySessionStore()),
        cli_factory=factory,
        semaphore=asyncio.Semaphore(1),
        structured_session_store=tmux_runner._session_store,
    )
    await service._session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))
    state = tmux_runner._session_store.get_or_create(session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f")
    state.phase = SessionPhase.WAITING_FOR_INPUT
    state.turns.append(ConversationTurn(turn_id="turn-1", role="assistant", text="\n你好\n", is_complete=True))
    tmux_runner._session_store._persist(state)

    record = TaskRecord(
        task_id="task-1",
        session_id="session-1",
        user_id=1,
        provider="claude_code",
        prompt="hi",
        workdir=str(tmp_path),
        timeout_sec=10,
        status=TaskStatus.SUCCEEDED,
    )
    await service._task_store.add(record)

    router = DummyRouter()
    register_status_handler(router, task_service=service)
    handler = router.handlers[0]
    message = DummyMessage("/status task-1")
    command = SimpleNamespace(args="task-1")

    await handler(message, command)

    assert message.answers
    assert "structured_session:" in message.answers[0]
    assert "phase: waiting_for_input" in message.answers[0]
    assert "terminal_mode" not in message.answers[0]
    assert "terminal_id" not in message.answers[0]


@pytest.mark.asyncio
async def test_permission_handlers_approve_current_pending_request(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    hook_socket_server = DummyHookSocketServer()
    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=SessionService(MemorySessionStore()),
        cli_factory=factory,
        semaphore=asyncio.Semaphore(1),
        structured_session_store=tmux_runner._session_store,
        hook_socket_server=hook_socket_server,
    )
    await service._session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))
    state = tmux_runner._session_store.get_or_create(session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f")
    state.pending_permission = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    register_permission_handlers(router, task_service=service)
    approve_handler = router.handlers[0]
    message = DummyMessage("/approve")

    await approve_handler(message)

    assert hook_socket_server.calls == [("tool-1", "allow", None)]
    assert message.answers == ["已批准权限请求: Bash"]


@pytest.mark.asyncio
async def test_permission_handlers_report_stale_pending_request(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    hook_socket_server = DummyHookSocketServer(respond_ok=False)
    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=SessionService(MemorySessionStore()),
        cli_factory=factory,
        semaphore=asyncio.Semaphore(1),
        structured_session_store=tmux_runner._session_store,
        hook_socket_server=hook_socket_server,
    )
    await service._session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))
    state = tmux_runner._session_store.get_or_create(session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f")
    state.pending_permission = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    register_permission_handlers(router, task_service=service)
    approve_handler = router.handlers[0]
    message = DummyMessage("/approve")

    await approve_handler(message)

    assert hook_socket_server.calls == [("tool-1", "allow", None)]
    assert message.answers == ["批准失败: 待处理权限请求已失效，请等待 Claude 重新发起"]


@pytest.mark.asyncio
async def test_router_text_chat_awaits_background_stream_task(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    session_service = SessionService(MemorySessionStore())
    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=session_service,
        cli_factory=factory,
        semaphore=asyncio.Semaphore(1),
        structured_session_store=tmux_runner._session_store,
    )
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    task = asyncio.create_task(asyncio.sleep(0))
    await task
    run_mock = AsyncMock(return_value=task)
    monkeypatch.setattr("app.bot.router.run_prompt_and_stream", run_mock)

    router = create_router(settings=make_settings(tmp_path), task_service=service, session_service=session_service)
    text_handler = router.message.handlers[-1].callback
    message = DummyMessage("你好")

    await text_handler(message)

    run_mock.assert_awaited_once()
