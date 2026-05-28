import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.process.tmux_runner import TmuxRunner
from app.adapters.storage.memory import MemorySessionStore, MemoryTaskStore
from app.bot.handlers.command_permission import register_permission_handlers
from app.bot.handlers.command_user_question import register_user_question_handlers
from app.bot.handlers.command_session import register_session_handler
from app.bot.handlers.command_status import register_status_handler
from app.bot.router import create_router
from app.domain.models import TaskRecord, TaskStatus
from app.domain.session_models import (
    ConversationTurn,
    SessionEvent,
    SessionEventType,
    SessionPhase,
    PendingPermission,
    ToolCallRecord,
    ToolStatus,
)
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from app.services.permission_callback_registry import PermissionCallbackRegistry
from tests.fakes.cli import DummyHookSocketServer, StubAdapter, StubFactory, make_settings
from tests.fakes.telegram import DummyCallbackQuery, DummyMessage


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
async def test_permission_handlers_approve_current_pending_request(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    hook_socket_server = DummyHookSocketServer()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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
    state = tmux_runner._session_store.get_or_create(
        session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f"
    )
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
async def test_permission_handlers_deny_disables_all_auto_approve_state(tmp_path) -> None:
    from app.services.auto_approve_service import AutoApproveService, SlotClaimed

    class FakeTaskService:
        def __init__(self) -> None:
            self.denials: list[tuple[int, str, str | None]] = []

        async def get_structured_session(self, user_id: int, *, log_missing: bool = True):  # noqa: ANN202
            return SimpleNamespace(session_id="current-session")

        async def respond_to_pending_permission(self, *, user_id: int, decision: str, reason: str | None = None, **kwargs):  # noqa: ANN202, ARG002
            self.denials.append((user_id, decision, reason))
            return False, "no pending permission"

    service = FakeTaskService()
    auto_approve_service = AutoApproveService()
    await auto_approve_service.activate("other-session", user_id=1)
    slot = await auto_approve_service.try_claim_slot(session_id="slot-session", user_id=1)
    assert isinstance(slot, SlotClaimed)

    router = DummyRouter()
    register_permission_handlers(router, task_service=service, auto_approve_service=auto_approve_service)
    deny_handler = router.handlers[1]
    message = DummyMessage("/deny", user_id=1)
    command = SimpleNamespace(args=None)

    await deny_handler(message, command)

    assert message.answers == ["已关闭自动批准，后续权限请求将正常提示"]
    assert service.denials == []
    assert not auto_approve_service.is_active("other-session", user_id=1)
    assert all(slot.holder_user_id != 1 for slot in auto_approve_service._slots.values())
    assert auto_approve_service.deny_epoch(1) == 1


@pytest.mark.asyncio
async def test_permission_handlers_report_stale_pending_request(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    hook_socket_server = DummyHookSocketServer(respond_ok=False)
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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
    state = tmux_runner._session_store.get_or_create(
        session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f"
    )
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
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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
    state = tmux_runner._session_store.get_or_create(
        session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f"
    )
    state.pending_permission = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    token = registry.register("tool-1")
    register_permission_handlers(router, task_service=service, permission_callback_registry=registry)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("权限请求")
    callback = DummyCallbackQuery(f"perm:allow:{token}", message=message)

    await callback_handler(callback)

    assert hook_socket_server.calls == [("tool-1", "allow", None)]
    assert message.answers == ["已批准权限请求: Bash"]
    assert message.edited_reply_markups == [None]
    assert callback.answers == [("已批准权限请求: Bash", False)]


@pytest.mark.asyncio
async def test_permission_callback_auto_approve_reports_session_ended_without_activation() -> None:
    from app.services.auto_approve_service import AutoApproveService

    class FakeTaskService:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str, str]] = []

        async def respond_to_pending_permission(self, *, user_id: int, decision: str, expected_tool_use_id: str, **kwargs):  # noqa: ANN202, ARG002
            self.calls.append((user_id, decision, expected_tool_use_id))
            return True, "已批准权限请求: Bash"

        async def get_structured_session(self, user_id: int, *, log_missing: bool = True):  # noqa: ANN202, ARG002
            return None

    class FakeSessionStore:
        def find_by_pending_tool_use_id(self, tool_use_id: str):  # noqa: ANN202
            return SimpleNamespace(session_id="ended-session")

        def get(self, session_id: str):  # noqa: ANN202
            return SimpleNamespace(user_id=1)

    service = FakeTaskService()
    auto_approve_service = AutoApproveService()
    await auto_approve_service.deactivate_all_for_session("ended-session")

    router = DummyRouter()
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    token = registry.register("tool-1")
    register_permission_handlers(
        router,
        task_service=service,
        auto_approve_service=auto_approve_service,
        structured_session_store=FakeSessionStore(),
        permission_callback_registry=registry,
    )
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("权限请求")
    callback = DummyCallbackQuery(f"perm:auto_approve:{token}", message=message)

    await callback_handler(callback)

    fallback = "已批准权限请求: Bash\n自动批准未开启：会话已结束"
    assert service.calls == [(1, "allow", "tool-1")]
    assert not auto_approve_service.is_active("ended-session", user_id=1)
    assert message.answers == [fallback]
    assert message.edited_reply_markups == [None]
    assert callback.answers == [(fallback, False)]


@pytest.mark.asyncio
async def test_external_permission_auto_approve_reports_session_ended_without_activation() -> None:
    from app.bot.handlers.external_permission import register_external_permission_handler
    from app.services.auto_approve_service import AutoApproveService
    from tests.fakes.telegram import DummyAnswerMessage

    class FakeHookSocketServer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

        async def get_session_id_for_tool_use_id(self, tool_use_id: str) -> str:
            return "ended-external-session"

        async def respond_to_permission(self, *, tool_use_id: str, decision: str, reason: str | None = None) -> bool:
            self.calls.append((tool_use_id, decision, reason))
            return True

    class FakeUnboundPermissionHandler:
        def is_unbound_permission(self, tool_use_id: str) -> bool:  # noqa: ARG002
            return False

    hook_socket_server = FakeHookSocketServer()
    auto_approve_service = AutoApproveService()
    await auto_approve_service.deactivate_all_for_session("ended-external-session")

    router = DummyRouter()
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    token = registry.register("tool-1")
    register_external_permission_handler(
        router,
        hook_socket_server=hook_socket_server,
        unbound_permission_handler=FakeUnboundPermissionHandler(),
        permission_callback_registry=registry,
        auto_approve_service=auto_approve_service,
    )
    callback_handler = router.callback_handlers[0]
    message = DummyAnswerMessage("Permission request")
    callback = DummyCallbackQuery(f"ext_perm:{token}:auto_approve", message=message)

    await callback_handler(callback)

    fallback = "Permission approved, but session ended; auto-approve was not activated."
    assert hook_socket_server.calls == [("tool-1", "allow", "auto-approve activated by user 1")]
    assert not auto_approve_service.is_active("ended-external-session", user_id=1)
    assert callback.answers == [(fallback, False)]
    assert message.edits == [f"Permission request\n\n{fallback}"]


@pytest.mark.asyncio
async def test_permission_callback_handler_rejects_stale_button(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    hook_socket_server = DummyHookSocketServer()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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
    state = tmux_runner._session_store.get_or_create(
        session_id="claude-session-1", workdir=str(tmp_path), terminal_id="user_1_36d00faeb25f"
    )
    state.pending_permission = PendingPermission(tool_use_id="tool-2", tool_name="Bash", tool_input={"command": "pwd"})
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    register_permission_handlers(router, task_service=service, permission_callback_registry=registry)
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("权限请求")
    callback = DummyCallbackQuery("perm:allow:missing", message=message)

    await callback_handler(callback)

    assert hook_socket_server.calls == []
    assert "权限按钮已失效" in message.answers[0]
    assert "重新触发" in message.answers[0]
    assert message.edited_reply_markups == []
    assert callback.answers == [("按钮已失效", True)]


@pytest.mark.asyncio
async def test_permission_callback_handler_rejects_cross_user_button(tmp_path) -> None:
    tmux_runner = TmuxRunner(data_dir=str(tmp_path))
    factory = StubFactory(StubAdapter(events=[]))
    factory._tmux_runner = tmux_runner
    factory._claude_tmux_enabled = True
    factory.get_claude_session_state = lambda session_id: tmux_runner.get_session_state(session_id)
    hook_socket_server = DummyHookSocketServer()
    service = TaskService(
        settings=make_settings(tmp_path, claude_tmux_mode=True),
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
    state.pending_permission = PendingPermission(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"})
    state.phase = SessionPhase.WAITING_FOR_APPROVAL
    tmux_runner._session_store._persist(state)

    router = DummyRouter()
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    token = registry.register("tool-1")
    register_permission_handlers(
        router,
        task_service=service,
        hook_socket_server=hook_socket_server,
        structured_session_store=tmux_runner._session_store,
        permission_callback_registry=registry,
    )
    callback_handler = router.callback_handlers[0]
    message = DummyMessage("权限请求", user_id=2)
    callback = DummyCallbackQuery(f"perm:allow:{token}", user_id=2, message=message)

    await callback_handler(callback)

    assert hook_socket_server.calls == [("tool-1", "allow", None)]
    assert message.answers == ["已批准权限请求: Bash"]
    assert message.edited_reply_markups == [None]
    assert callback.answers == [("已批准权限请求: Bash", False)]
    assert tmux_runner._session_store.get("claude-session-1").pending_permission is None


def test_permission_callback_data_uses_short_token_for_long_tool_use_id() -> None:
    from app.bot.handlers.command_permission import build_permission_callback_data, build_permission_keyboard

    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok12345", clock=lambda: 100.0)
    long_tool_use_id = "toolu_" + "x" * 200

    keyboard = build_permission_keyboard(tool_use_id=long_tool_use_id, permission_callback_registry=registry)
    callback_data = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert callback_data == [
        "perm:allow:tok12345",
        "perm:deny:tok12345",
        "perm:auto_approve:tok12345",
    ]
    assert all(data is not None and len(data.encode("utf-8")) <= 64 for data in callback_data)
    assert registry.resolve("tok12345") == long_tool_use_id
    assert build_permission_callback_data(decision="allow", token="tok12345") == "perm:allow:tok12345"


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
