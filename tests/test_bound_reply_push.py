from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.adapters.storage.file_session_store import FileSessionStore
from app.bootstrap_mixins import HookHandlingMixin, JsonlSyncMixin
from app.domain.external_session_models import ExternalBinding
from app.domain.hook_models import HookEvent
from app.domain.session_models import ConversationTurn, SessionState
from app.services.background_task_registry import BackgroundTaskRegistry
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_reply_delivery_pump import ExternalReplyDeliveryPump, ExternalReplyDrainResult
from app.services.session_store import SessionStore

_BOUND_AT = datetime(2026, 1, 2, tzinfo=UTC)


def _turn(
    turn_id: str,
    role: str,
    text: str,
    *,
    complete: bool = True,
    at: datetime = _BOUND_AT,
) -> ConversationTurn:
    return ConversationTurn(
        turn_id=turn_id,
        role=role,
        text=text,
        is_complete=complete,
        started_at=at,
        ended_at=at if complete else None,
    )


def _stop_event() -> HookEvent:
    return HookEvent(
        session_id="sess-123456",
        cwd="/home/user/project",
        event="Stop",
        status="waiting_for_input",
    )


async def _wait_until(predicate: Callable[[], bool], *, timeout_sec: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.005)


class _LockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, key: str):
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield


class _ReplyContainer(JsonlSyncMixin, HookHandlingMixin):
    def __init__(self, state: SessionState, *, cursor: str | None = None) -> None:
        self.settings = SimpleNamespace(external_push_reply_enabled=True)
        self.structured_session_store = MagicMock()
        self.structured_session_store.get.return_value = state
        self.binding = SimpleNamespace(
            title="绑定会话",
            user_id=42,
            cwd="/home/user/project",
            bound_at=_BOUND_AT,
            last_pushed_reply_turn_id=cursor,
            reply_cursor_initialized=True,
            ended_at=None,
        )
        self.external_binding_store = MagicMock()
        self.external_binding_store.get_binding.return_value = self.binding

        def set_reply_cursor(_session_id: str, turn_id: str | None) -> bool:
            if self.binding.reply_cursor_initialized and self.binding.last_pushed_reply_turn_id == turn_id:
                return False
            self.binding.last_pushed_reply_turn_id = turn_id
            self.binding.reply_cursor_initialized = True
            return True

        self.external_binding_store.set_reply_cursor.side_effect = set_reply_cursor
        self.push_notifier = MagicMock()
        self.push_notifier.notify_assistant_reply = AsyncMock(return_value=True)
        self.push_notifier.notify_session_end = AsyncMock(return_value=True)
        self.external_reply_delivery_pump = MagicMock()
        self.external_reply_delivery_pump.stop = AsyncMock()
        self.session_supervisor = MagicMock()
        self.session_supervisor.forget = AsyncMock()
        self.sync_claude_session = AsyncMock()
        self._schedule_jsonl_sync = MagicMock()
        self._jsonl_sync_locks = _LockRegistry()
        self._session_event_locks = _LockRegistry()
        self._external_reply_delivery_locks = _LockRegistry()


class _BaselineContainer(JsonlSyncMixin):
    def __init__(self, state: SessionState) -> None:
        self.sync_claude_session = AsyncMock()
        self._session_event_locks = _LockRegistry()
        self._external_reply_delivery_locks = _LockRegistry()
        self.structured_session_store = MagicMock()
        self.structured_session_store.get.return_value = state
        self.binding = SimpleNamespace(
            bound_at=_BOUND_AT,
            last_pushed_reply_turn_id=None,
            reply_cursor_initialized=True,
        )
        self.external_binding_store = MagicMock()
        self.external_binding_store.get_binding.return_value = self.binding
        self.external_reply_delivery_pump = MagicMock()


@pytest.mark.asyncio
async def test_pushes_all_completed_assistant_turns_after_cursor() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "first"),
            _turn("u1", "user", "next"),
            _turn("a2", "assistant", "second"),
            _turn("a3", "assistant", "third"),
        ],
    )
    container = _ReplyContainer(state, cursor="a1")

    result = await container._push_bound_assistant_replies(_stop_event(), user_id=42)

    assert result == ExternalReplyDrainResult.DELIVERED
    assert container.push_notifier.notify_assistant_reply.await_count == 2
    calls = container.push_notifier.notify_assistant_reply.await_args_list
    assert [call.kwargs["text"] for call in calls] == ["second", "third"]
    assert all(call.kwargs["title"] == "绑定会话" for call in calls)
    assert container.binding.last_pushed_reply_turn_id == "a3"
    assert container.external_binding_store.set_reply_cursor.call_args_list == [
        call("sess-123456", "a2"),
        call("sess-123456", "a3"),
    ]


@pytest.mark.asyncio
async def test_legacy_binding_initializes_cursor_without_replaying_history() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "old"),
            _turn("a2", "assistant", "latest"),
        ],
    )
    container = _ReplyContainer(state)
    container.binding.reply_cursor_initialized = False

    result = await container._push_bound_assistant_replies(_stop_event(), user_id=42)

    assert result == ExternalReplyDrainResult.NO_NEW_REPLY
    container.push_notifier.notify_assistant_reply.assert_not_awaited()
    container.external_binding_store.set_reply_cursor.assert_called_once_with("sess-123456", "a2")
    assert container.binding.reply_cursor_initialized is True
    assert container.binding.last_pushed_reply_turn_id == "a2"


@pytest.mark.asyncio
async def test_no_cursor_pushes_all_replies_completed_after_binding() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "old", at=_BOUND_AT - timedelta(seconds=1)),
            _turn("a2", "assistant", "first new", at=_BOUND_AT + timedelta(seconds=1)),
            _turn("a3", "assistant", "second new", at=_BOUND_AT + timedelta(seconds=2)),
        ],
    )
    container = _ReplyContainer(state)

    result = await container._push_bound_assistant_replies(_stop_event(), user_id=42)

    assert result == ExternalReplyDrainResult.DELIVERED
    calls = container.push_notifier.notify_assistant_reply.await_args_list
    assert [item.kwargs["text"] for item in calls] == ["first new", "second new"]
    assert container.binding.last_pushed_reply_turn_id == "a3"


@pytest.mark.asyncio
async def test_missing_cursor_recovers_all_replies_after_binding() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "old", at=_BOUND_AT - timedelta(seconds=1)),
            _turn("a2", "assistant", "first new", at=_BOUND_AT + timedelta(seconds=1)),
            _turn("a3", "assistant", "second new", at=_BOUND_AT + timedelta(seconds=2)),
        ],
    )
    container = _ReplyContainer(state, cursor="missing")

    result = await container._push_bound_assistant_replies(_stop_event(), user_id=42)

    assert result == ExternalReplyDrainResult.DELIVERED
    calls = container.push_notifier.notify_assistant_reply.await_args_list
    assert [item.kwargs["text"] for item in calls] == ["first new", "second new"]
    assert container.binding.last_pushed_reply_turn_id == "a3"


@pytest.mark.asyncio
async def test_delivery_failure_keeps_cursor_for_retry() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "first"),
            _turn("a2", "assistant", "second"),
        ],
    )
    container = _ReplyContainer(state, cursor="a1")
    container.push_notifier.notify_assistant_reply.return_value = False

    result = await container._push_bound_assistant_replies(_stop_event(), user_id=42)

    assert result == ExternalReplyDrainResult.DELIVERY_FAILED
    assert container.binding.last_pushed_reply_turn_id == "a1"
    container.external_binding_store.set_reply_cursor.assert_not_called()


@pytest.mark.asyncio
async def test_delivery_failure_does_not_skip_later_replies() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "first"),
            _turn("a2", "assistant", "second"),
            _turn("a3", "assistant", "third"),
            _turn("a4", "assistant", "fourth"),
        ],
    )
    container = _ReplyContainer(state, cursor="a1")
    container.push_notifier.notify_assistant_reply.side_effect = [True, False, True, True]

    first_result = await container._drain_bound_assistant_replies("sess-123456")

    assert first_result == ExternalReplyDrainResult.DELIVERY_FAILED
    assert [item.kwargs["text"] for item in container.push_notifier.notify_assistant_reply.await_args_list] == [
        "second",
        "third",
    ]
    assert container.binding.last_pushed_reply_turn_id == "a2"

    second_result = await container._drain_bound_assistant_replies("sess-123456")

    assert second_result == ExternalReplyDrainResult.DELIVERED
    assert [item.kwargs["text"] for item in container.push_notifier.notify_assistant_reply.await_args_list] == [
        "second",
        "third",
        "third",
        "fourth",
    ]
    assert container.binding.last_pushed_reply_turn_id == "a4"


@pytest.mark.asyncio
async def test_stop_delivery_failure_requests_retry_without_end_notification() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "first"),
            _turn("a2", "assistant", "second"),
        ],
    )
    container = _ReplyContainer(state, cursor="a1")
    container.push_notifier.notify_assistant_reply.return_value = False

    await container._notify_bound_external_event(_stop_event(), user_id=42)

    container.push_notifier.notify_session_end.assert_not_awaited()
    container.external_reply_delivery_pump.request_settle.assert_called_once_with(
        session_id="sess-123456",
        cwd="/home/user/project",
    )


@pytest.mark.asyncio
async def test_concurrent_stop_pushes_each_reply_once() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "first"),
            _turn("a2", "assistant", "second"),
        ],
    )
    container = _ReplyContainer(state, cursor="a1")

    async def send_reply(**_kwargs) -> bool:
        await asyncio.sleep(0.01)
        return True

    container.push_notifier.notify_assistant_reply.side_effect = send_reply
    results = await asyncio.gather(
        container._push_bound_assistant_replies(_stop_event(), user_id=42),
        container._push_bound_assistant_replies(_stop_event(), user_id=42),
    )

    assert results.count(ExternalReplyDrainResult.DELIVERED) == 1
    assert results.count(ExternalReplyDrainResult.NO_NEW_REPLY) == 1
    container.push_notifier.notify_assistant_reply.assert_awaited_once()
    container.external_binding_store.set_reply_cursor.assert_called_once_with("sess-123456", "a2")


@pytest.mark.asyncio
async def test_unbind_and_rebind_wait_for_in_flight_reply_delivery(tmp_path) -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "first"),
            _turn("a2", "assistant", "second"),
        ],
    )
    container = _ReplyContainer(state, cursor="a1")
    binding_store = ExternalBindingStore(tmp_path)
    old_binding = ExternalBinding(
        session_id="sess-123456",
        user_id=42,
        cwd="/home/user/project",
        bound_at=_BOUND_AT,
        jsonl_path=None,
        title="旧绑定",
        last_pushed_reply_turn_id="a1",
    )
    binding_store.save_binding(old_binding)
    container.external_binding_store = binding_store
    send_started = asyncio.Event()
    allow_send = asyncio.Event()

    async def send_reply(**_kwargs) -> bool:
        send_started.set()
        await allow_send.wait()
        return True

    container.push_notifier.notify_assistant_reply.side_effect = send_reply
    drain_task = asyncio.create_task(container._drain_bound_assistant_replies("sess-123456"))
    await send_started.wait()

    remove_task = asyncio.create_task(container._remove_external_binding("sess-123456", old_binding.binding_id))
    await asyncio.sleep(0)
    new_binding = ExternalBinding(
        session_id="sess-123456",
        user_id=99,
        cwd="/home/user/new-project",
        bound_at=_BOUND_AT + timedelta(seconds=1),
        jsonl_path=None,
        title="新绑定",
    )
    save_task = asyncio.create_task(container._save_external_binding(new_binding))
    await asyncio.sleep(0)

    assert not remove_task.done()
    assert not save_task.done()

    allow_send.set()
    result, removed, saved = await asyncio.gather(drain_task, remove_task, save_task)

    assert result == ExternalReplyDrainResult.DELIVERED
    assert removed is old_binding
    assert saved is True
    current = binding_store.get_binding("sess-123456")
    assert current is new_binding
    assert current.last_pushed_reply_turn_id is None
    assert await container._remove_external_binding("sess-123456", old_binding.binding_id) is None
    assert binding_store.get_binding("sess-123456") is new_binding
    container.push_notifier.notify_assistant_reply.assert_awaited_once_with(
        user_id=42,
        session_id="sess-123456",
        text="second",
        title="旧绑定",
        turn_id="a2",
    )
    container.external_reply_delivery_pump.stop.assert_awaited_once_with("sess-123456")
    container.session_supervisor.forget.assert_awaited_once_with("sess-123456")


@pytest.mark.parametrize(
    ("new_activity", "new_pid"),
    [
        (_BOUND_AT + timedelta(minutes=1), 1234),
        (_BOUND_AT, 5678),
    ],
)
@pytest.mark.asyncio
async def test_cleanup_snapshot_does_not_remove_refreshed_same_generation(
    tmp_path,
    new_activity: datetime,
    new_pid: int,
) -> None:
    container = _ReplyContainer(SessionState(session_id="sess-123456"))
    binding_store = ExternalBindingStore(tmp_path)
    binding = ExternalBinding(
        session_id="sess-123456",
        user_id=42,
        cwd="/home/user/project",
        bound_at=_BOUND_AT,
        jsonl_path=None,
        pid=1234,
    )
    binding_store.save_binding(binding)
    container.external_binding_store = binding_store
    expected_activity = binding.last_activity_at

    async with container._external_reply_delivery_locks.lock("sess-123456"):
        remove_task = asyncio.create_task(
            container._remove_external_binding(
                "sess-123456",
                binding.binding_id,
                expected_activity,
                binding.pid,
            )
        )
        await asyncio.sleep(0)
        binding_store.touch_activity(
            "sess-123456",
            new_activity,
            pid=new_pid,
        )

    assert await remove_task is None
    current = binding_store.get_binding("sess-123456")
    assert current is binding
    assert current.pid == new_pid
    assert current.last_activity_at == new_activity
    container.external_reply_delivery_pump.stop.assert_not_awaited()
    container.session_supervisor.forget.assert_not_awaited()


@pytest.mark.asyncio
async def test_ended_stop_leaves_finalization_to_delivery_pump() -> None:
    state = SessionState(session_id="sess-123456")
    container = _ReplyContainer(state)
    event = HookEvent(
        session_id="sess-123456",
        cwd="/home/user/project",
        event="Stop",
        status="ended",
    )

    await container._notify_bound_external_event(event, user_id=42)

    container.sync_claude_session.assert_not_awaited()
    container.external_reply_delivery_pump.stop.assert_not_awaited()
    container.push_notifier.notify_session_end.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalizer_removes_ended_binding_only_after_end_notification_succeeds() -> None:
    container = _ReplyContainer(SessionState(session_id="sess-123456"))
    container.binding.ended_at = _BOUND_AT

    result = await container._finalize_bound_external_session("sess-123456")

    assert result is True
    container.push_notifier.notify_session_end.assert_awaited_once_with(
        user_id=42,
        session_id="sess-123456",
        cwd="/home/user/project",
    )
    container.external_binding_store.remove_binding.assert_called_once_with("sess-123456")
    container.push_notifier.discard_assistant_reply_progress.assert_called_once_with("sess-123456")
    container.session_supervisor.forget.assert_awaited_once_with("sess-123456")


@pytest.mark.asyncio
async def test_finalizer_skips_pending_replies_when_reply_push_is_disabled() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "pending", at=_BOUND_AT + timedelta(seconds=1))],
    )
    container = _ReplyContainer(state)
    container.settings.external_push_reply_enabled = False
    container.binding.reply_cursor_initialized = False
    container.binding.ended_at = _BOUND_AT + timedelta(seconds=2)

    result = await container._finalize_bound_external_session("sess-123456")

    assert result is True
    container.push_notifier.notify_assistant_reply.assert_not_awaited()
    container.push_notifier.notify_session_end.assert_awaited_once_with(
        user_id=42,
        session_id="sess-123456",
        cwd="/home/user/project",
    )
    container.external_binding_store.remove_binding.assert_called_once_with("sess-123456")


@pytest.mark.asyncio
async def test_finalizer_keeps_ended_binding_when_end_notification_fails() -> None:
    container = _ReplyContainer(SessionState(session_id="sess-123456"))
    container.binding.ended_at = _BOUND_AT
    container.push_notifier.notify_session_end.return_value = False

    result = await container._finalize_bound_external_session("sess-123456")

    assert result is False
    container.external_binding_store.remove_binding.assert_not_called()
    container.push_notifier.discard_assistant_reply_progress.assert_not_called()
    container.session_supervisor.forget.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalizer_defers_when_synced_reply_is_still_pending() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "first", at=_BOUND_AT),
            _turn("a2", "assistant", "pending", at=_BOUND_AT + timedelta(seconds=1)),
        ],
    )
    container = _ReplyContainer(state, cursor="a1")
    container.binding.ended_at = _BOUND_AT + timedelta(seconds=2)

    result = await container._finalize_bound_external_session("sess-123456")

    assert result is False
    container.push_notifier.notify_session_end.assert_not_awaited()
    container.external_binding_store.remove_binding.assert_not_called()


@pytest.mark.asyncio
async def test_finalizer_waits_for_in_flight_jsonl_sync_before_checking_pending_reply() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "first", at=_BOUND_AT)],
    )
    container = _ReplyContainer(state, cursor="a1")
    container.binding.ended_at = _BOUND_AT + timedelta(seconds=2)

    async with container._jsonl_sync_locks.lock("sess-123456"):
        finalize_task = asyncio.create_task(container._finalize_bound_external_session("sess-123456"))
        await asyncio.sleep(0)
        assert not finalize_task.done()
        state.turns.append(
            _turn(
                "a2",
                "assistant",
                "pending",
                at=_BOUND_AT + timedelta(seconds=1),
            )
        )

    assert await finalize_task is False
    container.push_notifier.notify_session_end.assert_not_awaited()
    container.external_binding_store.remove_binding.assert_not_called()


@pytest.mark.asyncio
async def test_session_end_retries_failed_reply_before_removing_binding(tmp_path) -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "first", at=_BOUND_AT),
            _turn("a2", "assistant", "final", at=_BOUND_AT + timedelta(seconds=1)),
        ],
    )
    container = _ReplyContainer(state, cursor="a1")
    binding_store = ExternalBindingStore(tmp_path)
    binding_store.save_binding(
        ExternalBinding(
            session_id="sess-123456",
            user_id=42,
            cwd="/home/user/project",
            bound_at=_BOUND_AT,
            jsonl_path=None,
            title="绑定会话",
            ended_at=_BOUND_AT + timedelta(seconds=2),
            last_pushed_reply_turn_id="a1",
            reply_cursor_initialized=True,
        )
    )
    container.external_binding_store = binding_store
    container.push_notifier.notify_assistant_reply.side_effect = [False, True]
    background_tasks = BackgroundTaskRegistry(label="test-bound-reply-finalize")
    pump = ExternalReplyDeliveryPump(
        session_store=SessionStore(FileSessionStore(str(tmp_path / "states"))),
        binding_store=binding_store,
        background_tasks=background_tasks,
        sync_callback=container.sync_claude_session,
        drain_callback=container._drain_bound_assistant_replies,
        finalize_callback=container._finalize_bound_external_session,
        settle_delays=(),
        retry_delays=(0.01,),
        idle_check_sec=0.05,
    )
    container.external_reply_delivery_pump = pump

    pump.ensure(session_id="sess-123456", cwd="/home/user/project")

    await _wait_until(lambda: binding_store.get_binding("sess-123456") is None)
    assert container.push_notifier.notify_assistant_reply.await_count == 2
    container.push_notifier.notify_session_end.assert_awaited_once()
    container.session_supervisor.forget.assert_awaited_once_with("sess-123456")
    await _wait_until(lambda: background_tasks.active_count == 0)


@pytest.mark.asyncio
async def test_session_end_settle_delivers_late_final_reply_before_notification(tmp_path) -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "first", at=_BOUND_AT)],
    )
    container = _ReplyContainer(state, cursor="a1")
    binding_store = ExternalBindingStore(tmp_path)
    binding_store.save_binding(
        ExternalBinding(
            session_id="sess-123456",
            user_id=42,
            cwd="/home/user/project",
            bound_at=_BOUND_AT,
            jsonl_path=None,
            title="绑定会话",
            ended_at=_BOUND_AT + timedelta(seconds=2),
            last_pushed_reply_turn_id="a1",
            reply_cursor_initialized=True,
        )
    )
    container.external_binding_store = binding_store
    sync_count = 0

    async def delayed_sync(session_id: str, cwd: str) -> None:
        nonlocal sync_count
        sync_count += 1
        if sync_count == 2:
            state.turns.append(_turn("a2", "assistant", "late final", at=_BOUND_AT + timedelta(seconds=1)))

    container.sync_claude_session = AsyncMock(side_effect=delayed_sync)
    background_tasks = BackgroundTaskRegistry(label="test-bound-reply-finalize")
    pump = ExternalReplyDeliveryPump(
        session_store=SessionStore(FileSessionStore(str(tmp_path / "states"))),
        binding_store=binding_store,
        background_tasks=background_tasks,
        sync_callback=container.sync_claude_session,
        drain_callback=container._drain_bound_assistant_replies,
        finalize_callback=container._finalize_bound_external_session,
        settle_delays=(0.01, 0.01),
        retry_delays=(0.01,),
        idle_check_sec=0.05,
    )
    container.external_reply_delivery_pump = pump

    pump.ensure(session_id="sess-123456", cwd="/home/user/project")

    await _wait_until(lambda: binding_store.get_binding("sess-123456") is None)
    container.push_notifier.notify_assistant_reply.assert_awaited_once_with(
        user_id=42,
        session_id="sess-123456",
        text="late final",
        title="绑定会话",
        turn_id="a2",
    )
    container.push_notifier.notify_session_end.assert_awaited_once()
    await _wait_until(lambda: background_tasks.active_count == 0)


@pytest.mark.asyncio
async def test_stop_without_new_reply_requests_settle_without_end_notification() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "first")],
    )
    container = _ReplyContainer(state, cursor="a1")

    await container._notify_bound_external_event(_stop_event(), user_id=42)

    container.push_notifier.notify_assistant_reply.assert_not_awaited()
    container.push_notifier.notify_session_end.assert_not_awaited()
    container.external_reply_delivery_pump.request_settle.assert_called_once_with(
        session_id="sess-123456",
        cwd="/home/user/project",
    )


@pytest.mark.asyncio
async def test_file_synced_revision_delivers_reply_without_stop(tmp_path) -> None:
    session_store = SessionStore(FileSessionStore(str(tmp_path)))
    state = session_store.get_or_create(session_id="sess-123456", workdir="/home/user/project")
    state.turns = [_turn("a1", "assistant", "first")]
    session_store.save(state)
    container = _ReplyContainer(state, cursor="a1")
    container.structured_session_store = session_store
    background_tasks = BackgroundTaskRegistry(label="test-bound-reply")
    container.external_reply_delivery_pump = ExternalReplyDeliveryPump(
        session_store=session_store,
        binding_store=container.external_binding_store,
        background_tasks=background_tasks,
        sync_callback=container.sync_claude_session,
        drain_callback=container._drain_bound_assistant_replies,
        finalize_callback=container._finalize_bound_external_session,
        settle_delays=(0.01,),
        retry_delays=(0.01,),
        idle_check_sec=0.05,
    )
    container.external_reply_delivery_pump.ensure(session_id="sess-123456", cwd="/home/user/project")
    await asyncio.sleep(0)

    state.turns.append(_turn("a2", "assistant", "second"))
    session_store.save(state)

    await _wait_until(lambda: container.push_notifier.notify_assistant_reply.await_count == 1)
    assert container.binding.last_pushed_reply_turn_id == "a2"
    await container.external_reply_delivery_pump.stop_all()


@pytest.mark.asyncio
async def test_stop_delayed_reply_is_delivered_without_next_stop(tmp_path) -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "first")],
    )
    container = _ReplyContainer(state, cursor="a1")
    sync_count = 0

    async def delayed_sync(session_id: str, cwd: str) -> None:
        nonlocal sync_count
        sync_count += 1
        if sync_count == 2:
            state.turns.append(_turn("a2", "assistant", "second"))

    container.sync_claude_session = AsyncMock(side_effect=delayed_sync)
    background_tasks = BackgroundTaskRegistry(label="test-bound-reply")
    container.external_reply_delivery_pump = ExternalReplyDeliveryPump(
        session_store=SessionStore(FileSessionStore(str(tmp_path))),
        binding_store=container.external_binding_store,
        background_tasks=background_tasks,
        sync_callback=container.sync_claude_session,
        drain_callback=container._drain_bound_assistant_replies,
        finalize_callback=container._finalize_bound_external_session,
        settle_delays=(0.01,),
        retry_delays=(0.01,),
        idle_check_sec=0.05,
    )

    await container._notify_bound_external_event(_stop_event(), user_id=42)
    await _wait_until(lambda: container.push_notifier.notify_assistant_reply.await_count == 1)

    container.push_notifier.notify_assistant_reply.assert_awaited_once_with(
        user_id=42,
        session_id="sess-123456",
        text="second",
        title="绑定会话",
        turn_id="a2",
    )
    assert container.binding.last_pushed_reply_turn_id == "a2"
    container.push_notifier.notify_session_end.assert_not_awaited()
    await container.external_reply_delivery_pump.stop_all()


@pytest.mark.asyncio
async def test_stop_settles_after_immediate_delivery_to_capture_final_reply(tmp_path) -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "first")],
    )
    container = _ReplyContainer(state, cursor="a1")
    sync_count = 0

    async def delayed_sync(session_id: str, cwd: str) -> None:
        nonlocal sync_count
        sync_count += 1
        if sync_count == 1:
            state.turns.append(_turn("a2", "assistant", "intermediate"))
        elif sync_count == 2:
            state.turns.append(_turn("a3", "assistant", "final"))

    container.sync_claude_session = AsyncMock(side_effect=delayed_sync)
    background_tasks = BackgroundTaskRegistry(label="test-bound-reply")
    container.external_reply_delivery_pump = ExternalReplyDeliveryPump(
        session_store=SessionStore(FileSessionStore(str(tmp_path))),
        binding_store=container.external_binding_store,
        background_tasks=background_tasks,
        sync_callback=container.sync_claude_session,
        drain_callback=container._drain_bound_assistant_replies,
        finalize_callback=container._finalize_bound_external_session,
        settle_delays=(0.01,),
        retry_delays=(0.01,),
        idle_check_sec=0.05,
    )

    await container._notify_bound_external_event(_stop_event(), user_id=42)
    await _wait_until(lambda: container.push_notifier.notify_assistant_reply.await_count == 2)

    assert [item.kwargs["text"] for item in container.push_notifier.notify_assistant_reply.await_args_list] == [
        "intermediate",
        "final",
    ]
    assert container.binding.last_pushed_reply_turn_id == "a3"
    container.push_notifier.notify_session_end.assert_not_awaited()
    await container.external_reply_delivery_pump.stop_all()


@pytest.mark.asyncio
async def test_disabled_reply_push_skips_sync() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "first")],
    )
    container = _ReplyContainer(state)
    container.settings.external_push_reply_enabled = False

    result = await container._push_bound_assistant_replies(_stop_event(), user_id=42)

    assert result == ExternalReplyDrainResult.NO_NEW_REPLY
    container.sync_claude_session.assert_not_awaited()
    container.push_notifier.notify_assistant_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_binding_sync_baselines_latest_completed_assistant_turn() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("u1", "user", "question", at=_BOUND_AT - timedelta(seconds=3)),
            _turn("a1", "assistant", "old reply", at=_BOUND_AT - timedelta(seconds=2)),
            _turn("a2", "assistant", "latest old reply", at=_BOUND_AT - timedelta(seconds=1)),
            _turn("a3", "assistant", "new reply", at=_BOUND_AT + timedelta(seconds=1)),
        ],
    )
    container = _BaselineContainer(state)

    await container._sync_and_baseline_external_reply("sess-123456", "/home/user/project")

    container.sync_claude_session.assert_awaited_once_with("sess-123456", "/home/user/project")
    container.external_binding_store.set_reply_cursor.assert_called_once_with("sess-123456", "a2")
    container.external_reply_delivery_pump.ensure.assert_called_once_with(
        session_id="sess-123456",
        cwd="/home/user/project",
    )


@pytest.mark.asyncio
async def test_binding_sync_failure_still_starts_background_recovery() -> None:
    state = SessionState(session_id="sess-123456")
    container = _BaselineContainer(state)
    container.sync_claude_session.side_effect = RuntimeError("sync failed")
    container.session_supervisor = MagicMock()

    await container._sync_and_baseline_external_reply("sess-123456", "/home/user/project")

    container.session_supervisor.watch.assert_called_once_with(
        session_id="sess-123456",
        workdir="/home/user/project",
    )
    container.session_supervisor.schedule_jsonl_sync.assert_called_once_with(
        "sess-123456",
        "/home/user/project",
    )
    container.external_reply_delivery_pump.request_settle.assert_called_once_with(
        session_id="sess-123456",
        cwd="/home/user/project",
    )


@pytest.mark.asyncio
async def test_binding_sync_failure_during_shutdown_does_not_restart_background_work() -> None:
    state = SessionState(session_id="sess-123456")
    container = _BaselineContainer(state)
    container.sync_claude_session.side_effect = RuntimeError("sync failed")
    container.session_supervisor = MagicMock()
    container._stopping = True

    await container._sync_and_baseline_external_reply("sess-123456", "/home/user/project")

    container.session_supervisor.watch.assert_not_called()
    container.session_supervisor.schedule_jsonl_sync.assert_not_called()
    container.external_reply_delivery_pump.request_settle.assert_not_called()


@pytest.mark.asyncio
async def test_binding_baseline_does_not_overwrite_advanced_cursor() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "old reply", at=_BOUND_AT - timedelta(seconds=1))],
    )
    container = _BaselineContainer(state)
    container.binding.last_pushed_reply_turn_id = "a2"

    await container._sync_and_baseline_external_reply("sess-123456", "/home/user/project")

    container.external_binding_store.set_reply_cursor.assert_not_called()


@pytest.mark.asyncio
async def test_reply_completed_at_bind_time_is_baselined_not_pushed() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[
            _turn("a1", "assistant", "at boundary", at=_BOUND_AT),
            _turn("a2", "assistant", "after binding", at=_BOUND_AT + timedelta(seconds=1)),
        ],
    )
    baseline_container = _BaselineContainer(state)

    await baseline_container._sync_and_baseline_external_reply("sess-123456", "/home/user/project")

    baseline_container.external_binding_store.set_reply_cursor.assert_called_once_with("sess-123456", "a1")

    reply_container = _ReplyContainer(state, cursor="a1")
    result = await reply_container._push_bound_assistant_replies(_stop_event(), user_id=42)

    assert result == ExternalReplyDrainResult.DELIVERED
    reply_container.push_notifier.notify_assistant_reply.assert_awaited_once()
    assert reply_container.push_notifier.notify_assistant_reply.call_args.kwargs["text"] == "after binding"
