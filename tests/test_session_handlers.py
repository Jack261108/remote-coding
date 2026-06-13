import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.process.tmux_runner import TmuxRunner
from app.adapters.storage.memory import MemorySessionStore, MemoryTaskStore
from app.bot.handlers.command_permission import register_permission_handlers
from app.bot.handlers.command_session import register_session_handler
from app.bot.handlers.command_status import register_status_handler
from app.bot.handlers.command_user_question import register_user_question_handlers
from app.bot.handlers.external_permission import register_external_permission_handler
from app.bot.router import create_router
from app.domain.models import TaskRecord, TaskStatus
from app.domain.session_models import (
    ConversationTurn,
    SessionEvent,
    SessionEventType,
    SessionPhase,
    ToolCallRecord,
    ToolStatus,
)
from app.domain.user_question_models import UserQuestionOption, UserQuestionPrompt
from app.services.external_user_question_state import ExternalUserQuestionState, PendingExternalUserQuestion
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from tests.fakes.cli import StubAdapter, StubFactory, make_settings
from tests.fakes.telegram import DummyCallbackQuery, DummyMessage


class _EventObserverStub:
    """Minimal stub mimicking aiogram EventObserver for decorator + middleware usage."""

    def __init__(self, handlers: list) -> None:
        self._handlers = handlers

    def __call__(self, *args, **kwargs):
        def decorator(fn):
            self._handlers.append(fn)
            return fn

        return decorator

    def middleware(self, middleware):  # noqa: ANN001
        pass


class DummyRouter:
    def __init__(self) -> None:
        self.handlers = []
        self.callback_handlers = []
        self.message = _EventObserverStub(self.handlers)
        self.callback_query = _EventObserverStub(self.callback_handlers)


@pytest.mark.asyncio
async def test_session_handler_renders_structured_snapshot(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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
async def test_session_handler_rejects_missing_workdir(tmp_path) -> None:
    factory = StubFactory(StubAdapter(events=[]))
    service = TaskService(
        settings=make_settings(tmp_path),
        task_store=MemoryTaskStore(),
        session_service=SessionService(MemorySessionStore()),
        cli_factory=factory,
        semaphore=asyncio.Semaphore(1),
    )
    session_service = service._session_service
    missing_workdir = tmp_path / "missing"

    router = DummyRouter()
    register_session_handler(router, task_service=service, session_service=session_service)
    handler = router.handlers[0]
    message = DummyMessage(f"/session claude_code {missing_workdir}")

    await handler(message)

    assert message.answers == [f"workdir 不存在或不是目录: {missing_workdir.resolve()}"]
    assert await session_service.get(1) is None


@pytest.mark.asyncio
async def test_status_handler_renders_structured_snapshot(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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
    state = tmux_runner._session_store.get_or_create(
        session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f"
    )
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
async def test_permission_handlers_delegate_text_commands_to_gateway() -> None:
    class FakeGateway:
        def __init__(self) -> None:
            self.approve_user_ids: list[int] = []
            self.deny_calls: list[tuple[int, str | None]] = []

        async def handle_approve_command(self, *, user_id: int) -> str:
            self.approve_user_ids.append(user_id)
            return "approved via gateway"

        async def handle_deny_command(self, *, user_id: int, reason: str | None = None) -> str:
            self.deny_calls.append((user_id, reason))
            return "denied via gateway"

    gateway = FakeGateway()
    router = DummyRouter()
    register_permission_handlers(router, permission_gateway=gateway)

    approve_message = DummyMessage("/approve", user_id=11)
    await router.handlers[0](approve_message)
    deny_message = DummyMessage("/deny no", user_id=22)
    await router.handlers[1](deny_message, SimpleNamespace(args="no"))

    assert gateway.approve_user_ids == [11]
    assert gateway.deny_calls == [(22, "no")]
    assert approve_message.answers == ["approved via gateway"]
    assert deny_message.answers == ["denied via gateway"]


@pytest.mark.asyncio
async def test_permission_callback_handler_delegates_to_gateway_response() -> None:
    class FakeGateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def handle_callback(self, *, data: str, user_id: int):  # noqa: ANN202
            self.calls.append((data, user_id))
            return SimpleNamespace(
                alert_text="gateway alert",
                show_alert=True,
                edit_message_text="",
                clear_keyboard=True,
            )

    gateway = FakeGateway()
    router = DummyRouter()
    register_permission_handlers(router, permission_gateway=gateway)
    message = DummyMessage("权限请求")
    callback = DummyCallbackQuery("perm:tok12345:allow", user_id=7, message=message)

    await router.callback_handlers[0](callback)

    assert gateway.calls == [("perm:tok12345:allow", 7)]
    assert message.edited_reply_markups == [None]
    assert callback.answers == [("gateway alert", True)]


@pytest.mark.asyncio
async def test_external_permission_callback_delegates_reformatted_payload_to_gateway() -> None:
    class FakeGateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def handle_callback(self, *, data: str, user_id: int):  # noqa: ANN202
            self.calls.append((data, user_id))
            return SimpleNamespace(alert_text="已批准", show_alert=False, edit_message_text="", clear_keyboard=True)

    gateway = FakeGateway()
    router = DummyRouter()
    register_external_permission_handler(
        router,
        hook_socket_server=SimpleNamespace(respond_to_permission=AsyncMock(return_value=True)),
        unbound_permission_handler=SimpleNamespace(),
        permission_gateway=gateway,
    )

    message = DummyMessage("Permission request")
    callback = DummyCallbackQuery("ext_perm:tok12345:allow", user_id=321, message=message)

    await router.callback_handlers[0](callback)

    assert gateway.calls == [("perm:tok12345:allow", 321)]
    assert callback.answers == [("已批准", False)]
    assert message.edited_reply_markups == [None]


@pytest.mark.asyncio
async def test_external_user_question_callback_still_uses_existing_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_inject(pane_id: str, *, option_index: int, submit_after: bool, tmux_bin: str = "tmux") -> tuple[bool, str]:
        assert pane_id == "%1"
        assert option_index == 0
        assert submit_after is True
        return True, ""

    monkeypatch.setattr("app.adapters.process.pty_injector.inject_option_selection", fake_inject)

    hook_socket_server = SimpleNamespace(respond_to_permission=AsyncMock(return_value=True))
    external_uq_state = ExternalUserQuestionState()
    external_uq_state.store(
        PendingExternalUserQuestion(
            tool_use_id="tool-question",
            session_id="external-session",
            user_id=1,
            pid=123,
            pane_id="%1",
            prompts=(
                UserQuestionPrompt(
                    tool_use_id="tool-question",
                    question_index=0,
                    total_questions=1,
                    question="Pick one",
                    options=(UserQuestionOption(label="A"),),
                ),
            ),
        )
    )

    router = DummyRouter()
    register_external_permission_handler(
        router,
        hook_socket_server=hook_socket_server,
        unbound_permission_handler=SimpleNamespace(),
        permission_gateway=SimpleNamespace(handle_callback=AsyncMock()),
        external_uq_state=external_uq_state,
    )

    message = DummyMessage("Question")
    callback = DummyCallbackQuery("ext_uq:tool-question:0", user_id=1, message=message)

    await router.callback_handlers[1](callback)

    hook_socket_server.respond_to_permission.assert_awaited_once_with(
        tool_use_id="tool-question",
        decision="allow",
        reason="AskUserQuestion answered via Telegram by user 1",
    )
    assert callback.answers == [("✅ Selected: A", False)]
    assert external_uq_state.get("tool-question") is None


@pytest.mark.asyncio
async def test_external_user_question_callback_rejects_invalidated_session(monkeypatch: pytest.MonkeyPatch) -> None:
    injected: list[str] = []

    async def fake_inject(pane_id: str, *, option_index: int, submit_after: bool, tmux_bin: str = "tmux") -> tuple[bool, str]:
        injected.append(pane_id)
        return True, ""

    monkeypatch.setattr("app.adapters.process.pty_injector.inject_option_selection", fake_inject)

    hook_socket_server = SimpleNamespace(respond_to_permission=AsyncMock(return_value=True))
    external_uq_state = ExternalUserQuestionState()
    external_uq_state.store(
        PendingExternalUserQuestion(
            tool_use_id="tool-question",
            session_id="external-session",
            user_id=1,
            pid=123,
            pane_id="%1",
            prompts=(
                UserQuestionPrompt(
                    tool_use_id="tool-question",
                    question_index=0,
                    total_questions=1,
                    question="Pick one",
                    options=(UserQuestionOption(label="A"),),
                ),
            ),
        )
    )
    assert external_uq_state.invalidate_session("external-session") == 1

    router = DummyRouter()
    register_external_permission_handler(
        router,
        hook_socket_server=hook_socket_server,
        unbound_permission_handler=SimpleNamespace(),
        permission_gateway=SimpleNamespace(handle_callback=AsyncMock()),
        external_uq_state=external_uq_state,
    )

    callback = DummyCallbackQuery("ext_uq:tool-question:0", user_id=1, message=DummyMessage("Question"))

    await router.callback_handlers[1](callback)

    assert callback.answers == [("Question expired or already answered", True)]
    assert injected == []
    hook_socket_server.respond_to_permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_question_callback_handler_records_choice_and_prompts_next_question(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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
    state = tmux_runner._session_store.get_or_create(
        session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f"
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
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    register_user_question_handlers(router, task_service=service)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("需要你选择")
    callback = DummyCallbackQuery("ask:tool-ask-1:0:0", message=message)

    await callback_handler(callback)

    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == [("user_1_36d00faeb25f", str(tmp_path), 0, False)]
    assert message.answers[0] == "已记录选择: 当前相关改动(推荐)"
    assert "问题: 2/2" in message.answers[1]
    assert callback.answers == [("已记录选择: 当前相关改动(推荐)", False)]
    assert tmux_runner._session_store.get("claude-session-1").structured_user_question_key == "tool-ask-1:1"


@pytest.mark.asyncio
async def test_user_question_callback_handler_rejects_cross_user_button(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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
    await service._session_service.switch(
        user_id=2,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )
    await service.bind_claude_session(user_id=2, claude_session_id="claude-session-2", workdir=str(tmp_path))
    state = tmux_runner._session_store.get_or_create(
        session_id="claude-session-1",
        user_id=1,
        workdir=str(tmp_path),
        terminal_id="user_1_36d00faeb25f",
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
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    register_user_question_handlers(router, task_service=service)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("需要你选择", user_id=2)
    callback = DummyCallbackQuery("ask:tool-ask-1:0:0", user_id=2, message=message)

    await callback_handler(callback)

    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == []
    assert message.answers == ["选择失败: 当前没有待处理的选择题"]
    assert message.edited_reply_markups == []
    assert callback.answers == [("当前没有待处理的选择题", True)]
    assert tmux_runner._session_store.get("claude-session-1").structured_user_question_key is None


@pytest.mark.asyncio
async def test_user_question_callback_handler_toggles_multi_select_and_submits(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
        task_store=MemoryTaskStore(),
        session_service=SessionService(MemorySessionStore()),
        cli_factory=factory,
        semaphore=asyncio.Semaphore(1),
        structured_session_store=tmux_runner._session_store,
    )
    session = await service._session_service.switch(
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
                    ],
                    "multiSelect": True,
                }
            ]
        },
        status=ToolStatus.RUNNING,
    )
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    register_user_question_handlers(router, task_service=service)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("需要你选择")

    toggle_callback = DummyCallbackQuery("ask:toggle:tool-ask-multi:0:0", message=message)
    await callback_handler(toggle_callback)

    assert message.answers == []
    assert len(message.edited_reply_markups) == 1
    toggle_markup = message.edited_reply_markups[0]
    assert toggle_markup is not None
    assert [button.text for row in toggle_markup.inline_keyboard for button in row] == [
        "☑ 保留日志",
        "☐ 保留测试",
        "提交选择",
    ]
    assert toggle_callback.answers == [("已选择: 保留日志", False)]

    submit_callback = DummyCallbackQuery("ask:submit:tool-ask-multi:0", message=message)
    await callback_handler(submit_callback)

    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == [(session.terminal_id, str(tmp_path), 0, False)]
    assert factory._user_question_multi_select_advances == [(session.terminal_id, str(tmp_path), True)]
    assert message.answers == ["已提交你的选择，Claude 继续执行中"]
    assert message.edited_reply_markups[-1] is None
    assert submit_callback.answers == [("已提交你的选择，Claude 继续执行中", False)]


@pytest.mark.asyncio
async def test_router_text_chat_answers_pending_user_question_instead_of_creating_new_task(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    session_service = SessionService(MemorySessionStore())
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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

    router = create_router(settings=make_settings(tmp_path, claude_tmux_mode=True), task_service=service, session_service=session_service)
    text_handler = router.message.handlers[-1].callback
    message = DummyMessage("直接删除")

    await text_handler(message)

    run_mock.assert_not_awaited()
    assert factory._interactive_inputs == []
    assert factory._user_question_option_actions == [(session.terminal_id, str(tmp_path), 1, True)]
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
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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

    router = create_router(settings=make_settings(tmp_path, claude_tmux_mode=True), task_service=service, session_service=session_service)
    text_handler = router.message.handlers[-1].callback
    message = DummyMessage("你好")

    await text_handler(message)

    run_mock.assert_awaited_once()
