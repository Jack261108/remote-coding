from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.bootstrap_mixins import HookHandlingMixin, JsonlSyncMixin, _BoundReplyPushResult
from app.domain.hook_models import HookEvent
from app.domain.session_models import ConversationTurn, SessionState

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


class _LockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, key: str):
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield


class _ReplyContainer(HookHandlingMixin):
    def __init__(self, state: SessionState, *, cursor: str | None = None) -> None:
        self.settings = SimpleNamespace(external_push_reply_enabled=True)
        self.structured_session_store = MagicMock()
        self.structured_session_store.get.return_value = state
        self.binding = SimpleNamespace(
            title="绑定会话",
            bound_at=_BOUND_AT,
            last_pushed_reply_turn_id=cursor,
            reply_cursor_initialized=True,
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
        self.sync_claude_session = AsyncMock()
        self._schedule_jsonl_sync = MagicMock()
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

    assert result == _BoundReplyPushResult.DELIVERED
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

    assert result == _BoundReplyPushResult.NO_NEW_REPLY
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

    assert result == _BoundReplyPushResult.DELIVERED
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

    assert result == _BoundReplyPushResult.DELIVERED
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

    assert result == _BoundReplyPushResult.DELIVERY_FAILED
    assert container.binding.last_pushed_reply_turn_id == "a1"
    container.external_binding_store.set_reply_cursor.assert_not_called()


@pytest.mark.asyncio
async def test_stop_delivery_failure_uses_existing_end_notification() -> None:
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

    container.push_notifier.notify_session_end.assert_awaited_once_with(
        user_id=42,
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

    assert results.count(_BoundReplyPushResult.DELIVERED) == 1
    assert results.count(_BoundReplyPushResult.NO_NEW_REPLY) == 1
    container.push_notifier.notify_assistant_reply.assert_awaited_once()
    container.external_binding_store.set_reply_cursor.assert_called_once_with("sess-123456", "a2")


@pytest.mark.asyncio
async def test_ended_stop_skips_immediate_reply_sync() -> None:
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
    container.push_notifier.notify_session_end.assert_awaited_once_with(
        user_id=42,
        session_id="sess-123456",
        cwd="/home/user/project",
    )


@pytest.mark.asyncio
async def test_stop_without_new_reply_uses_existing_end_notification() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "first")],
    )
    container = _ReplyContainer(state, cursor="a1")

    await container._notify_bound_external_event(_stop_event(), user_id=42)

    container.push_notifier.notify_assistant_reply.assert_not_awaited()
    container.push_notifier.notify_session_end.assert_awaited_once_with(
        user_id=42,
        session_id="sess-123456",
        cwd="/home/user/project",
    )


@pytest.mark.asyncio
async def test_disabled_reply_push_skips_sync() -> None:
    state = SessionState(
        session_id="sess-123456",
        turns=[_turn("a1", "assistant", "first")],
    )
    container = _ReplyContainer(state)
    container.settings.external_push_reply_enabled = False

    result = await container._push_bound_assistant_replies(_stop_event(), user_id=42)

    assert result == _BoundReplyPushResult.NO_NEW_REPLY
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

    assert result == _BoundReplyPushResult.DELIVERED
    reply_container.push_notifier.notify_assistant_reply.assert_awaited_once()
    assert reply_container.push_notifier.notify_assistant_reply.call_args.kwargs["text"] == "after binding"
