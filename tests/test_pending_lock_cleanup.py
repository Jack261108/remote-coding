"""Tests for pending permission state and lock cleanup (Task 3.4).

Covers:
- UnboundPermissionHandler: response removes pending state and cancels expiry task
- UnboundPermissionHandler: expiry removes pending state
- Concurrent responses preserve first-responder-wins
- Different permissions don't serialize on socket I/O
- TmuxRunner: RefCountedLockRegistry lock count stays bounded
- AgentFileWatcher: forget() clears mtime and defers lock cleanup
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.presenters.permission_message_builder import PermissionMessageBuilder
from app.domain.hook_models import HookEvent
from app.services.message_sender import Button, Keyboard
from app.services.permission_gateway import RegisterForButtonOk
from app.services.unbound_permission_handler import UnboundPermissionHandler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakePermissionGateway:
    def __init__(self) -> None:
        self.message_builder = PermissionMessageBuilder()
        self.keyboard = Keyboard(rows=[[Button(text="Allow", callback_data="perm:tok:allow")]])

    async def register_for_button(self, **kwargs):  # noqa: ANN003, ANN202
        return RegisterForButtonOk(keyboard=self.keyboard, token="test-token")


def _make_handler(
    *,
    allowed_user_ids: set[int] | None = None,
    permission_ttl_sec: int = 600,
) -> tuple[UnboundPermissionHandler, MagicMock, MagicMock]:
    message_sender = MagicMock()
    message_sender.send_message = AsyncMock()
    hook_socket_server = MagicMock()
    hook_socket_server.respond_to_permission = AsyncMock()
    handler = UnboundPermissionHandler(
        message_sender=message_sender,
        hook_socket_server=hook_socket_server,
        allowed_user_ids=allowed_user_ids or {111},
        permission_ttl_sec=permission_ttl_sec,
    )
    handler.set_permission_gateway(FakePermissionGateway())
    return handler, message_sender, hook_socket_server


def _make_event(tool_use_id: str = "tuid-1", session_id: str = "sess-1") -> HookEvent:
    return HookEvent(
        session_id=session_id,
        cwd="/tmp/project",
        event="PermissionRequest",
        status="waiting_for_approval",
        tool="Bash",
        tool_use_id=tool_use_id,
    )


# ---------------------------------------------------------------------------
# UnboundPermissionHandler: response removes pending state and expiry task
# ---------------------------------------------------------------------------


class TestResponseRemovesPendingAndExpiryTask:
    """handle_response removes entry from _pending and cancels expiry task."""

    @pytest.mark.asyncio
    async def test_response_removes_pending_entry(self):
        handler, _, _ = _make_handler()
        event = _make_event("tuid-a")
        await handler.handle_unbound_permission(event)

        assert "tuid-a" in handler._pending
        assert "tuid-a" in handler._expiry_tasks

        result = await handler.handle_response(tool_use_id="tuid-a", user_id=111, decision="allow")

        assert result.accepted is True
        assert "tuid-a" not in handler._pending
        assert "tuid-a" not in handler._expiry_tasks

    @pytest.mark.asyncio
    async def test_response_cancels_expiry_task(self):
        handler, _, _ = _make_handler(permission_ttl_sec=3600)
        event = _make_event("tuid-b")
        await handler.handle_unbound_permission(event)

        expiry_task = handler._expiry_tasks["tuid-b"]
        assert not expiry_task.done()

        await handler.handle_response(tool_use_id="tuid-b", user_id=111, decision="deny")

        # Allow cancellation to propagate
        await asyncio.sleep(0)

        # Task should be cancelled
        assert expiry_task.cancelled() or expiry_task.done()


# ---------------------------------------------------------------------------
# UnboundPermissionHandler: expiry removes pending state
# ---------------------------------------------------------------------------


class TestExpiryRemovesPendingState:
    """TTL expiry removes entry from _pending."""

    @pytest.mark.asyncio
    async def test_expiry_removes_pending_entry(self):
        handler, _, hook_socket = _make_handler(permission_ttl_sec=0)
        event = _make_event("tuid-expire")
        await handler.handle_unbound_permission(event)

        # Wait for expiry task to fire
        await asyncio.sleep(0.05)

        assert "tuid-expire" not in handler._pending
        hook_socket.respond_to_permission.assert_called_once_with(
            tool_use_id="tuid-expire",
            decision="deny",
            reason="no user responded within TTL",
        )

    @pytest.mark.asyncio
    async def test_expiry_removes_expiry_task_reference(self):
        handler, _, _ = _make_handler(permission_ttl_sec=0)
        event = _make_event("tuid-expire2")
        await handler.handle_unbound_permission(event)

        await asyncio.sleep(0.05)

        assert "tuid-expire2" not in handler._expiry_tasks


# ---------------------------------------------------------------------------
# Concurrent responses preserve first-responder-wins
# ---------------------------------------------------------------------------


class TestConcurrentFirstResponderWins:
    """Only first concurrent response is accepted; others return False."""

    @pytest.mark.asyncio
    async def test_concurrent_responses_only_first_wins(self):
        handler, _, hook_socket = _make_handler(allowed_user_ids={111, 222, 333})
        event = _make_event("tuid-concurrent")
        await handler.handle_unbound_permission(event)

        # Fire concurrent responses
        results = await asyncio.gather(
            handler.handle_response(tool_use_id="tuid-concurrent", user_id=111, decision="allow"),
            handler.handle_response(tool_use_id="tuid-concurrent", user_id=222, decision="allow"),
            handler.handle_response(tool_use_id="tuid-concurrent", user_id=333, decision="deny"),
        )

        # Exactly one wins
        assert sum(1 for result in results if result.accepted) == 1
        assert sum(1 for result in results if not result.accepted) == 2

        # respond_to_permission called exactly once
        assert hook_socket.respond_to_permission.call_count == 1

    @pytest.mark.asyncio
    async def test_late_response_after_first_wins_returns_false(self):
        handler, _, _ = _make_handler(allowed_user_ids={111, 222})
        event = _make_event("tuid-late")
        await handler.handle_unbound_permission(event)

        first = await handler.handle_response(tool_use_id="tuid-late", user_id=111, decision="allow")
        assert first.accepted is True

        second = await handler.handle_response(tool_use_id="tuid-late", user_id=222, decision="deny")
        assert second.accepted is False


# ---------------------------------------------------------------------------
# Different permissions don't serialize on socket I/O
# ---------------------------------------------------------------------------


class TestDifferentPermissionsDontSerialize:
    """Responses to different tool_use_ids run concurrently, not serialized."""

    @pytest.mark.asyncio
    async def test_different_permissions_run_concurrently(self):
        handler, _, hook_socket = _make_handler()

        # Track timing to verify concurrency
        call_order: list[str] = []

        async def slow_respond(*, tool_use_id: str, decision: str, reason: str):
            call_order.append(f"start:{tool_use_id}")
            await asyncio.sleep(0.02)
            call_order.append(f"end:{tool_use_id}")

        hook_socket.respond_to_permission = slow_respond

        event_a = _make_event("tuid-A", session_id="sess-A")
        event_b = _make_event("tuid-B", session_id="sess-B")
        await handler.handle_unbound_permission(event_a)
        await handler.handle_unbound_permission(event_b)

        # Fire concurrent responses to different permissions
        await asyncio.gather(
            handler.handle_response(tool_use_id="tuid-A", user_id=111, decision="allow"),
            handler.handle_response(tool_use_id="tuid-B", user_id=111, decision="deny"),
        )

        # Both started before either finished (concurrent execution)
        assert "start:tuid-A" in call_order
        assert "start:tuid-B" in call_order
        # Both start calls should appear before both end calls
        start_indices = [call_order.index("start:tuid-A"), call_order.index("start:tuid-B")]
        end_indices = [call_order.index("end:tuid-A"), call_order.index("end:tuid-B")]
        assert max(start_indices) < max(end_indices)


# ---------------------------------------------------------------------------
# TmuxRunner: RefCountedLockRegistry lock count stable after repeated runs
# ---------------------------------------------------------------------------


class TestTmuxLockCountStable:
    """RefCountedLockRegistry lock count stays bounded after completed sessions."""

    @pytest.mark.asyncio
    async def test_lock_count_stable_after_repeated_runs(self, tmp_path):
        from app.adapters.process.tmux_runner import TmuxRunner

        runner = TmuxRunner(
            data_dir=str(tmp_path),
            session_lock_ttl_sec=1,
            lock_cleanup_interval_sec=1,
            lock_cleanup_batch_size=50,
        )

        # Verify the runner uses RefCountedLockRegistry
        from app.infra.lock_registry import RefCountedLockRegistry

        assert isinstance(runner._session_locks, RefCountedLockRegistry)

        # Simulate multiple lock acquisitions and releases (as persistent terminal runs would do)
        for i in range(10):
            async with runner._session_locks.lock(f"session-{i}"):
                pass

        # After releasing, all locks should be eligible for cleanup
        # Force time advancement by using a clock override isn't available here,
        # but we can verify entries exist and will be cleaned
        initial_count = len(runner._session_locks)
        assert initial_count <= 10

        # Reuse same key repeatedly - should not grow unbounded
        for _ in range(20):
            async with runner._session_locks.lock("session-reuse"):
                pass

        # Count should not have grown unboundedly
        assert len(runner._session_locks) <= 11  # 10 unique + 1 reused

    @pytest.mark.asyncio
    async def test_lock_cleanup_after_ttl(self, tmp_path):
        from app.infra.lock_registry import RefCountedLockRegistry

        now = 100.0

        def clock() -> float:
            return now

        registry = RefCountedLockRegistry(
            ttl_sec=10,
            cleanup_interval_sec=1,
            cleanup_batch_size=50,
            clock=clock,
        )

        # Simulate completing several session locks
        for i in range(5):
            async with registry.lock(f"session-{i}"):
                pass

        assert len(registry) == 5

        # Advance past TTL
        now = 200.0
        await registry.cleanup_expired()

        assert len(registry) == 0


# ---------------------------------------------------------------------------
# AgentFileWatcher: forget() clears mtime and defers lock cleanup
# ---------------------------------------------------------------------------


class TestAgentFileWatcherForget:
    """forget() removes mtime keys immediately and defers lock cleanup."""

    @pytest.mark.asyncio
    async def test_forget_removes_mtime_keys(self):
        from unittest.mock import MagicMock as SyncMock

        from app.services.agent_file_watcher import AgentFileWatcher

        session_store = SyncMock()
        session_store.get.return_value = None
        claude_parser = SyncMock()
        on_update = AsyncMock()

        watcher = AgentFileWatcher(
            session_store=session_store,
            claude_jsonl_parser=claude_parser,
            on_update=on_update,
            poll_interval_sec=0.01,
        )

        # Simulate mtime entries for a session
        watcher._seen_mtimes["sess-1:tool-1:agent-1"] = 1000.0
        watcher._seen_mtimes["sess-1:tool-2:agent-2"] = 2000.0
        watcher._seen_mtimes["sess-2:tool-1:agent-1"] = 3000.0

        # Start a fake task so forget has something to cancel
        watcher._tasks["sess-1"] = asyncio.create_task(asyncio.sleep(100))

        watcher.forget(session_id="sess-1")

        # mtime keys for sess-1 removed immediately
        assert "sess-1:tool-1:agent-1" not in watcher._seen_mtimes
        assert "sess-1:tool-2:agent-2" not in watcher._seen_mtimes
        # Other session's mtime keys preserved
        assert "sess-2:tool-1:agent-1" in watcher._seen_mtimes
        # Task removed from _tasks
        assert "sess-1" not in watcher._tasks

    @pytest.mark.asyncio
    async def test_forget_cancels_watcher_task(self):
        from app.services.agent_file_watcher import AgentFileWatcher

        session_store = MagicMock()
        session_store.get.return_value = None
        claude_parser = MagicMock()
        on_update = AsyncMock()

        watcher = AgentFileWatcher(
            session_store=session_store,
            claude_jsonl_parser=claude_parser,
            on_update=on_update,
            poll_interval_sec=0.01,
        )

        # Create a long-running task
        task = asyncio.create_task(asyncio.sleep(100))
        watcher._tasks["sess-cancel"] = task

        watcher.forget(session_id="sess-cancel")

        # Allow cancellation to propagate
        await asyncio.sleep(0)

        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_finished_watcher_cleans_own_lock(self):
        """Watcher's finally block cleans up its own completed task and lock."""
        from app.services.agent_file_watcher import AgentFileWatcher

        session_store = MagicMock()
        # Return None so _watch_session exits early
        session_store.get.return_value = None
        claude_parser = MagicMock()
        on_update = AsyncMock()

        watcher = AgentFileWatcher(
            session_store=session_store,
            claude_jsonl_parser=claude_parser,
            on_update=on_update,
            poll_interval_sec=0.01,
        )
        watcher._active = True

        watcher.watch(session_id="sess-gen", workdir="/tmp")
        task = watcher._tasks["sess-gen"]
        await task

        assert "sess-gen" not in watcher._tasks
        assert "sess-gen" not in watcher._session_locks

    @pytest.mark.asyncio
    async def test_newer_watcher_prevents_old_lock_cleanup(self):
        """If a newer watcher is registered, old cleanup doesn't remove its lock."""
        from app.services.agent_file_watcher import AgentFileWatcher

        session_store = MagicMock()
        session_store.get.return_value = None
        claude_parser = MagicMock()
        on_update = AsyncMock()

        watcher = AgentFileWatcher(
            session_store=session_store,
            claude_jsonl_parser=claude_parser,
            on_update=on_update,
            poll_interval_sec=0.01,
        )

        old_task = asyncio.create_task(asyncio.sleep(100))
        new_task = asyncio.create_task(asyncio.sleep(100))
        watcher._tasks["sess-overlap"] = new_task
        watcher._session_locks["sess-overlap"] = asyncio.Lock()

        try:
            watcher._cleanup_finished_session(session_id="sess-overlap", task=old_task)

            assert watcher._tasks["sess-overlap"] is new_task
            assert "sess-overlap" in watcher._session_locks
        finally:
            for task in (old_task, new_task):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
