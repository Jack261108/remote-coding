import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardMarkup

from app.adapters.process.tmux_runner import TmuxRunner
from app.adapters.storage.memory import MemorySessionStore, MemoryTaskStore
from app.bot.handlers.command_permission import register_permission_handlers
from app.bot.handlers.command_user_question import register_user_question_handlers
from app.bot.handlers.command_session import register_session_handler
from app.bot.handlers.command_status import register_status_handler
from app.bot.router import create_router
from app.config.settings import Settings
from app.domain.models import TaskRecord, TaskStatus
from app.domain.session_models import ConversationTurn, SessionEvent, SessionEventType, SessionPhase, PendingPermission, ToolCallRecord, ToolStatus
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
        self.reply_markups: list[InlineKeyboardMarkup | None] = []
        self.edited_reply_markups: list[InlineKeyboardMarkup | None] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append(text)
        self.reply_markups.append(reply_markup)

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.edited_reply_markups.append(reply_markup)


class DummyCallbackQuery:
    def __init__(self, data: str, *, user_id: int = 1, message: DummyMessage | None = None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class DummyRouter:
    def __init__(self) -> None:
        self.handlers = []
        self.callback_handlers = []

    def message(self, *args, **kwargs):
        def decorator(fn):
            self.handlers.append(fn)
            return fn

        return decorator

    def callback_query(self, *args, **kwargs):
        def decorator(fn):
            self.callback_handlers.append(fn)
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
    session = await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))
    state = tmux_runner._session_store.get_or_create(
        session_id="claude-session-1",
        workdir=str(tmp_path),
        terminal_id=session.terminal_id,
    )
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
        claude_session_id="claude-session-1",
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
async def test_permission_callback_handler_approves_pending_request(tmp_path) -> None:
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
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("权限请求")
    callback = DummyCallbackQuery("perm:allow:tool-1", message=message)

    await callback_handler(callback)

    assert hook_socket_server.calls == [("tool-1", "allow", None)]
    assert message.answers == ["已批准权限请求: Bash"]
    assert message.edited_reply_markups == [None]
    assert callback.answers == [("已批准权限请求: Bash", False)]


@pytest.mark.asyncio
async def test_permission_callback_handler_rejects_stale_button(tmp_path) -> None:
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
    state.pending_permission = PendingPermission(tool_use_id="tool-2", tool_name="Bash", tool_input={"command": "pwd"})
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    register_permission_handlers(router, task_service=service)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("权限请求")
    callback = DummyCallbackQuery("perm:allow:tool-1", message=message)

    await callback_handler(callback)

    assert hook_socket_server.calls == []
    assert message.answers == ["权限操作失败: 这个权限按钮已经过期，请等待最新的权限请求"]
    assert message.edited_reply_markups == [None]
    assert callback.answers == [("这个权限按钮已经过期，请等待最新的权限请求", True)]


@pytest.mark.asyncio
async def test_user_question_callback_handler_records_choice_and_prompts_next_question(tmp_path) -> None:
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
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    register_user_question_handlers(router, task_service=service)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("需要你选择")
    callback = DummyCallbackQuery("ask:tool-ask-1:0:0", message=message)

    await callback_handler(callback)

    assert factory._interactive_inputs == []
    assert message.answers[0] == "已记录选择: 当前相关改动(推荐)"
    assert "问题: 2/2" in message.answers[1]
    assert callback.answers == [("已记录选择: 当前相关改动(推荐)", False)]


@pytest.mark.asyncio
async def test_router_text_chat_answers_pending_user_question_instead_of_creating_new_task(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    session = await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await service.bind_claude_session(user_id=1, claude_session_id="claude-session-1", workdir=str(tmp_path))
    state = tmux_runner._session_store.get_or_create(
        session_id="claude-session-1",
        workdir=str(tmp_path),
        terminal_id=session.terminal_id,
    )
    state.phase = SessionPhase.PROCESSING
    state.tool_calls["tool-ask-2"] = ToolCallRecord(
        tool_use_id="tool-ask-2",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "header": "处理方式",
                    "question": "这两条误写到项目级的记忆，你要我怎么处理？",
                    "options": [
                        {"label": "迁到全局(推荐)", "description": "保留记忆内容并迁移"},
                        {"label": "直接删除", "description": "删除项目级这两条记忆"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    tmux_runner._session_store._persist(state)

    run_mock = AsyncMock()
    monkeypatch.setattr("app.bot.router.run_prompt_and_stream", run_mock)

    router = create_router(settings=make_settings(tmp_path), task_service=service, session_service=session_service)
    text_handler = router.message.handlers[-1].callback
    message = DummyMessage("直接删除")

    await text_handler(message)

    run_mock.assert_not_awaited()
    assert factory._interactive_inputs == [(session.terminal_id, str(tmp_path), "直接删除")]
    assert message.answers == ["已提交你的选择，Claude 继续执行中"]


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
