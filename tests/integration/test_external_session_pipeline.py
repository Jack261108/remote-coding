"""Integration tests for the external session takeover pipeline.

These tests exercise the actual service interactions (not just mocks)
using temp directories for persistence.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.domain.hook_models import HookEvent
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.external_session_push_notifier import ExternalSessionPushNotifier
from app.services.permission_callback_registry import PermissionCallbackRegistry
from app.services.unbound_permission_handler import UnboundPermissionHandler


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    return tmp_path / "projects"


def _make_hook_event(
    *,
    session_id: str = "sess-abc123",
    cwd: str = "/home/user/project",
    event: str = "PreToolUse",
    status: str = "running_tool",
    pid: int | None = None,
    tool: str | None = None,
    tool_use_id: str | None = None,
    tool_input: dict | None = None,
) -> HookEvent:
    return HookEvent(
        session_id=session_id,
        cwd=cwd,
        event=event,
        status=status,
        pid=pid,
        tool=tool,
        tool_use_id=tool_use_id,
        tool_input=tool_input,
    )


class TestDiscoveryBindJsonlSync:
    """Discovery → bind → JSONL sync pipeline."""

    @pytest.mark.asyncio
    async def test_full_discovery_bind_sync_pipeline(self, tmp_data_dir: Path, projects_dir: Path) -> None:
        """Record hook events → verify in discovery → bind → verify binding created and JSONL sync triggered."""
        # Setup services
        discovery = ExternalSessionDiscoveryService()
        binding_store = ExternalBindingStore(data_dir=tmp_data_dir)
        sync_callback = AsyncMock()

        binder = ExternalSessionBinder(
            discovery=discovery,
            binding_store=binding_store,
            projects_dir=projects_dir,
            sync_callback=sync_callback,
        )

        session_id = "sess-abc123"
        cwd = "/home/user/project"
        user_id = 42

        # Step 1: Record hook events into discovery
        event = _make_hook_event(session_id=session_id, cwd=cwd)
        discovery.record_event(event)

        # Verify session appears in discovery
        unbound_sessions = discovery.list_unbound()
        assert len(unbound_sessions) == 1
        assert unbound_sessions[0].session_id == session_id
        assert unbound_sessions[0].cwd == cwd

        # Step 2: Bind the session
        result = await binder.bind(user_id=user_id, session_id=session_id)

        # Step 3: Verify binding created
        assert result.success is True
        assert result.session_id == session_id

        stored_binding = binding_store.get_binding(session_id)
        assert stored_binding is not None
        assert stored_binding.user_id == user_id
        assert stored_binding.cwd == cwd

        # Step 4: Verify session removed from discovery
        assert discovery.get(session_id) is None

        # Step 5: Verify sync_callback triggered with correct args
        sync_callback.assert_called_once_with(session_id, cwd)


class TestPermissionRequestForwarding:
    """Permission request forwarding for bound external sessions."""

    @pytest.mark.asyncio
    async def test_bound_session_permission_push_notification(self, tmp_data_dir: Path, projects_dir: Path) -> None:
        """Create binding, simulate PermissionRequest, verify push notifier called."""
        # Setup services
        discovery = ExternalSessionDiscoveryService()
        binding_store = ExternalBindingStore(data_dir=tmp_data_dir)
        sync_callback = AsyncMock()

        binder = ExternalSessionBinder(
            discovery=discovery,
            binding_store=binding_store,
            projects_dir=projects_dir,
            sync_callback=sync_callback,
        )

        session_id = "sess-perm001"
        cwd = "/home/user/myapp"
        user_id = 99

        # Setup: discover and bind
        event = _make_hook_event(session_id=session_id, cwd=cwd)
        discovery.record_event(event)
        result = await binder.bind(user_id=user_id, session_id=session_id)
        assert result.success is True

        # Create push notifier with mocked bot
        mock_bot = AsyncMock()
        push_notifier = ExternalSessionPushNotifier(
            bot=mock_bot,
            binding_store=binding_store,
        )

        # Simulate a PermissionRequest event for the bound session
        delivered = await push_notifier.notify_permission_request(
            user_id=user_id,
            session_id=session_id,
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
            tool_use_id="test-tool-use-id-123",
            cwd=cwd,
        )

        # Verify push notification was sent
        assert delivered is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == user_id
        assert session_id[:8] in call_kwargs["text"]


class TestUnboundPermissionBroadcast:
    """Unbound permission broadcast → first user responds → decision forwarded."""

    @pytest.mark.asyncio
    async def test_broadcast_and_first_responder_wins(self) -> None:
        """Broadcast to all users, respond from one user, verify decision forwarded."""
        mock_bot = AsyncMock()
        mock_hook_socket = AsyncMock()

        allowed_users = {100, 200, 300}

        handler = UnboundPermissionHandler(
            bot=mock_bot,
            hook_socket_server=mock_hook_socket,
            allowed_user_ids=allowed_users,
            permission_ttl_sec=60,
            permission_callback_registry=PermissionCallbackRegistry(ttl_sec=60),
        )

        session_id = "sess-unbound01"
        tool_use_id = "tooluse-xyz789"

        # Simulate a PermissionRequest from an unbound session
        event = _make_hook_event(
            session_id=session_id,
            cwd="/tmp/project",
            event="PermissionRequest",
            status="waiting_for_approval",
            tool="Write",
            tool_use_id=tool_use_id,
            tool_input={"command": "echo hello"},
        )

        # Step 1: Handle unbound permission → broadcasts to all users
        await handler.handle_unbound_permission(event)

        # Verify broadcast sent to all allowed users
        assert mock_bot.send_message.call_count == len(allowed_users)
        notified_chat_ids = {call.kwargs["chat_id"] for call in mock_bot.send_message.call_args_list}
        assert notified_chat_ids == allowed_users

        # Step 2: First user responds with "approve"
        first_response = await handler.handle_response(tool_use_id=tool_use_id, user_id=200, decision="approve")
        assert first_response.accepted is True

        # Verify decision forwarded to hook socket
        mock_hook_socket.respond_to_permission.assert_called_once_with(
            tool_use_id=tool_use_id,
            decision="approve",
            reason="responded by user 200",
        )

        # Step 3: Second user tries to respond (too late)
        second_response = await handler.handle_response(tool_use_id=tool_use_id, user_id=100, decision="deny")
        assert second_response.accepted is False

        # Verify only one decision forwarded
        mock_hook_socket.respond_to_permission.assert_called_once()


class TestServerRestartBindingsRestored:
    """Server restart → bindings restored from disk."""

    def test_bindings_survive_restart(self, tmp_data_dir: Path) -> None:
        """Save bindings via store, create new store instance, verify load_all matches."""
        from app.domain.external_session_models import ExternalBinding
        from app.domain.models import utc_now

        # Create store and save bindings
        store1 = ExternalBindingStore(data_dir=tmp_data_dir)

        now = utc_now()
        binding1 = ExternalBinding(
            session_id="sess-restart01",
            user_id=10,
            cwd="/home/alice/proj",
            bound_at=now,
            jsonl_path="/tmp/projects/-home-alice-proj/sess-restart01.jsonl",
        )
        binding2 = ExternalBinding(
            session_id="sess-restart02",
            user_id=20,
            cwd="/home/bob/work",
            bound_at=now,
            jsonl_path="/tmp/projects/-home-bob-work/sess-restart02.jsonl",
        )

        store1.save_binding(binding1)
        store1.save_binding(binding2)

        # Simulate server restart: create new store from same directory
        store2 = ExternalBindingStore(data_dir=tmp_data_dir)

        # Verify all bindings restored
        loaded = store2.load_all()
        assert len(loaded) == 2
        assert "sess-restart01" in loaded
        assert "sess-restart02" in loaded

        restored1 = loaded["sess-restart01"]
        assert restored1.user_id == 10
        assert restored1.cwd == "/home/alice/proj"
        assert restored1.jsonl_path == "/tmp/projects/-home-alice-proj/sess-restart01.jsonl"

        restored2 = loaded["sess-restart02"]
        assert restored2.user_id == 20
        assert restored2.cwd == "/home/bob/work"
        assert restored2.jsonl_path == "/tmp/projects/-home-bob-work/sess-restart02.jsonl"


class TestUnboundPermissionKeyboardToken:
    """Unbound permission keyboard uses external short token from registry."""

    @pytest.mark.asyncio
    async def test_unbound_permission_keyboard_uses_external_short_token(self) -> None:
        """Keyboard callback_data uses ext_perm:{token}:{decision} format, all <= 64 bytes,
        and registry resolves token back to full tool_use_id."""
        mock_bot = AsyncMock()
        mock_hook_socket = AsyncMock()

        registry = PermissionCallbackRegistry(
            ttl_sec=300,
            token_factory=lambda: "tok12345",
        )

        handler = UnboundPermissionHandler(
            bot=mock_bot,
            hook_socket_server=mock_hook_socket,
            allowed_user_ids={42},
            permission_ttl_sec=60,
            permission_callback_registry=registry,
        )

        # Use a long tool_use_id that would exceed 64 bytes with old format
        long_tool_use_id = "tooluse-" + "a" * 80

        event = HookEvent(
            session_id="sess-keyboard01",
            cwd="/tmp/proj",
            event="PermissionRequest",
            status="waiting_for_approval",
            tool="Bash",
            tool_use_id=long_tool_use_id,
            tool_input={"command": "echo hello"},
        )

        await handler.handle_unbound_permission(event)

        # Get the keyboard that was sent
        call_kwargs = mock_bot.send_message.call_args.kwargs
        keyboard = call_kwargs["reply_markup"]

        # Extract callback_data from all buttons
        callback_data = []
        for row in keyboard.inline_keyboard:
            for button in row:
                callback_data.append(button.callback_data)

        assert callback_data == [
            "ext_perm:tok12345:allow",
            "ext_perm:tok12345:deny",
            "ext_perm:tok12345:auto_approve",
        ]

        # All callback_data must be <= 64 bytes
        for cd in callback_data:
            assert len(cd.encode("utf-8")) <= 64

        # Registry must resolve token back to full tool_use_id
        assert registry.resolve("tok12345") == long_tool_use_id
