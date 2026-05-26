"""Property-based tests for UnboundPermissionHandler.

Feature: external-session-takeover
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.hook_models import HookEvent
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.permission_callback_registry import PermissionCallbackRegistry
from app.services.unbound_permission_handler import UnboundPermissionHandler

# --- Strategies ---

session_id_chars = st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
session_id_first_char = st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
session_ids = st.builds(
    lambda first, rest: first + rest,
    session_id_first_char,
    st.text(session_id_chars, min_size=1, max_size=20),
)

tool_use_ids = st.builds(
    lambda first, rest: first + rest,
    session_id_first_char,
    st.text(session_id_chars, min_size=1, max_size=20),
)

cwds = st.builds(
    lambda parts: "/" + "/".join(parts),
    st.lists(
        st.text(
            st.characters(whitelist_categories=("L", "N"), min_codepoint=65, max_codepoint=122),
            min_size=1,
            max_size=10,
        ),
        min_size=1,
        max_size=4,
    ),
)

user_ids = st.integers(min_value=1, max_value=999999999)

allowed_user_sets = st.frozensets(user_ids, min_size=1, max_size=5)

tool_names = st.sampled_from(["Read", "Write", "Bash", "Edit", "ListDir", "Grep"])


def _registry(token: str = "tokTest123") -> PermissionCallbackRegistry:
    return PermissionCallbackRegistry(ttl_sec=300, token_factory=lambda: token)


def permission_events(
    session_id_st=session_ids,
    tool_use_id_st=tool_use_ids,
):
    """Strategy for HookEvent representing a PermissionRequest."""
    return st.builds(
        lambda sid, cwd, tuid, tool: HookEvent(
            session_id=sid,
            cwd=cwd,
            event="PermissionRequest",
            status="waiting_for_approval",
            tool=tool,
            tool_use_id=tuid,
        ),
        sid=session_id_st,
        cwd=cwds,
        tuid=tool_use_id_st,
        tool=tool_names,
    )


# --- Property 11: Unbound permission broadcast to all allowed users ---


class TestUnboundPermissionBroadcast:
    """Property 11: Unbound permission broadcast to all allowed users.

    **Validates: Requirements 4.1, 4.2**

    When an unbound permission event is handled, every user in allowed_user_ids
    gets notified with a message containing session_id and cwd.
    """

    @settings(max_examples=100)
    @given(
        event=permission_events(),
        allowed_users=allowed_user_sets,
    )
    def test_all_allowed_users_notified(self, event: HookEvent, allowed_users: frozenset[int]):
        """All users in allowed set receive notification with session_id and cwd."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        hook_socket_server = MagicMock()
        hook_socket_server.respond_to_permission = AsyncMock()

        handler = UnboundPermissionHandler(
            bot=bot,
            hook_socket_server=hook_socket_server,
            allowed_user_ids=set(allowed_users),
            permission_callback_registry=_registry(),
        )

        asyncio.run(handler.handle_unbound_permission(event))

        # Every allowed user should have been sent a message
        notified_user_ids = {call.kwargs["chat_id"] for call in bot.send_message.call_args_list}
        assert notified_user_ids == set(allowed_users)

        # Each message should contain session short_id and cwd
        for call in bot.send_message.call_args_list:
            text = call.kwargs["text"]
            assert event.session_id[:8] in text
            assert event.cwd in text


# --- Property 12: First-responder-wins for unbound permissions ---


class TestFirstResponderWins:
    """Property 12: First-responder-wins for unbound permissions.

    **Validates: Requirements 4.3**

    Only the first response to an unbound permission request is accepted;
    all subsequent responses are rejected. respond_to_permission is called once.
    """

    @settings(max_examples=100)
    @given(
        event=permission_events(),
        responders=st.lists(user_ids, min_size=2, max_size=6),
    )
    def test_only_first_response_wins(self, event: HookEvent, responders: list[int]):
        """Only the first responder's decision is forwarded; others return False."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        hook_socket_server = MagicMock()
        hook_socket_server.respond_to_permission = AsyncMock()

        handler = UnboundPermissionHandler(
            bot=bot,
            hook_socket_server=hook_socket_server,
            allowed_user_ids={responders[0]},
            permission_callback_registry=_registry(),
        )

        asyncio.run(handler.handle_unbound_permission(event))

        tool_use_id = event.tool_use_id

        # First response should return True
        result_first = asyncio.run(handler.handle_response(tool_use_id=tool_use_id, user_id=responders[0], decision="approve"))
        assert result_first.accepted is True

        # All subsequent responses should return False
        for user_id in responders[1:]:
            result = asyncio.run(handler.handle_response(tool_use_id=tool_use_id, user_id=user_id, decision="approve"))
            assert result.accepted is False

        # respond_to_permission called exactly once
        assert hook_socket_server.respond_to_permission.call_count == 1


# --- Property 13: Permission approval doesn't auto-bind ---


class TestPermissionApprovalNoAutoBind:
    """Property 13: Permission approval doesn't auto-bind.

    **Validates: Requirements 4.4**

    After approving an unbound permission, the session remains in the
    discovery service's unbound list.
    """

    @settings(max_examples=100)
    @given(
        event=permission_events(),
        approver=user_ids,
    )
    def test_session_remains_in_discovery_after_approval(self, event: HookEvent, approver: int):
        """Approving a permission does not remove session from discovery."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        hook_socket_server = MagicMock()
        hook_socket_server.respond_to_permission = AsyncMock()

        discovery = ExternalSessionDiscoveryService()
        # Record session in discovery first
        discovery.record_event(event)

        handler = UnboundPermissionHandler(
            bot=bot,
            hook_socket_server=hook_socket_server,
            allowed_user_ids={approver},
            permission_callback_registry=_registry(),
        )

        # Handle permission and approve
        asyncio.run(handler.handle_unbound_permission(event))
        asyncio.run(handler.handle_response(tool_use_id=event.tool_use_id, user_id=approver, decision="approve"))

        # Session must still be in discovery (not auto-removed)
        unbound_ids = {s.session_id for s in discovery.list_unbound()}
        assert event.session_id in unbound_ids


# --- Property 14: TTL expiry auto-denies ---


class TestTTLExpiryAutoDenies:
    """Property 14: TTL expiry auto-denies.

    **Validates: Requirements 4.5**

    If no user responds within TTL, the permission is auto-denied via
    respond_to_permission with decision="deny".
    """

    @pytest.mark.asyncio
    async def test_ttl_expiry_triggers_auto_deny(self):
        """After TTL expires without response, permission is auto-denied."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        hook_socket_server = MagicMock()
        hook_socket_server.respond_to_permission = AsyncMock()

        handler = UnboundPermissionHandler(
            bot=bot,
            hook_socket_server=hook_socket_server,
            allowed_user_ids={12345},
            permission_ttl_sec=0,  # Expire immediately
            permission_callback_registry=_registry(),
        )

        event = HookEvent(
            session_id="sess001",
            cwd="/tmp/project",
            event="PermissionRequest",
            status="waiting_for_approval",
            tool="Bash",
            tool_use_id="tuid001",
        )

        await handler.handle_unbound_permission(event)

        # Wait for the expiry task to fire
        await asyncio.sleep(0.05)

        # respond_to_permission should have been called with "deny"
        hook_socket_server.respond_to_permission.assert_called_once_with(
            tool_use_id="tuid001",
            decision="deny",
            reason="no user responded within TTL",
        )

    @pytest.mark.asyncio
    async def test_ttl_expiry_prevents_late_response(self):
        """After TTL auto-deny, late user responses are rejected."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        hook_socket_server = MagicMock()
        hook_socket_server.respond_to_permission = AsyncMock()

        handler = UnboundPermissionHandler(
            bot=bot,
            hook_socket_server=hook_socket_server,
            allowed_user_ids={12345},
            permission_ttl_sec=0,
            permission_callback_registry=_registry(),
        )

        event = HookEvent(
            session_id="sess002",
            cwd="/tmp/work",
            event="PermissionRequest",
            status="waiting_for_approval",
            tool="Write",
            tool_use_id="tuid002",
        )

        await handler.handle_unbound_permission(event)
        await asyncio.sleep(0.05)

        # Late response should return False
        result = await handler.handle_response(tool_use_id="tuid002", user_id=12345, decision="approve")
        assert result.accepted is False


# --- Task 6: Response removes pending and expiry, concurrent first-responder-wins ---


class TestResponseRemovesPendingAndExpiry:
    """After handle_response, _pending and _expiry_tasks are cleaned up."""

    @pytest.mark.asyncio
    async def test_response_removes_unbound_pending_and_expiry_task(self):
        """handle_response returns accepted=True, forwarded=True and cleans up state."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        hook_socket_server = MagicMock()
        hook_socket_server.respond_to_permission = AsyncMock()

        handler = UnboundPermissionHandler(
            bot=bot,
            hook_socket_server=hook_socket_server,
            allowed_user_ids={42},
            permission_ttl_sec=60,
            permission_callback_registry=_registry(),
        )

        event = HookEvent(
            session_id="sess-cleanup01",
            cwd="/tmp/proj",
            event="PermissionRequest",
            status="waiting_for_approval",
            tool="Bash",
            tool_use_id="tuid-cleanup01",
        )

        await handler.handle_unbound_permission(event)
        assert "tuid-cleanup01" in handler._pending
        assert "tuid-cleanup01" in handler._expiry_tasks

        result = await handler.handle_response(tool_use_id="tuid-cleanup01", user_id=42, decision="allow")
        assert result.accepted is True
        assert result.forwarded is True

        # State must be cleaned up
        assert "tuid-cleanup01" not in handler._pending
        assert "tuid-cleanup01" not in handler._expiry_tasks


class TestExpiryRemovesPendingAndExpiry:
    """After TTL expiry, _pending and _expiry_tasks are cleaned up."""

    @pytest.mark.asyncio
    async def test_expiry_removes_unbound_pending_and_expiry_task(self):
        """With TTL=0, after short sleep _pending and _expiry_tasks are empty."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        hook_socket_server = MagicMock()
        hook_socket_server.respond_to_permission = AsyncMock()

        handler = UnboundPermissionHandler(
            bot=bot,
            hook_socket_server=hook_socket_server,
            allowed_user_ids={42},
            permission_ttl_sec=0,
            permission_callback_registry=_registry(),
        )

        event = HookEvent(
            session_id="sess-expiry01",
            cwd="/tmp/proj",
            event="PermissionRequest",
            status="waiting_for_approval",
            tool="Bash",
            tool_use_id="tuid-expiry01",
        )

        await handler.handle_unbound_permission(event)
        assert "tuid-expiry01" in handler._pending

        await asyncio.sleep(0.05)

        assert "tuid-expiry01" not in handler._pending
        assert "tuid-expiry01" not in handler._expiry_tasks


class TestConcurrentUnboundResponses:
    """Concurrent responses: exactly one accepted, respond_to_permission called once."""

    @pytest.mark.asyncio
    async def test_concurrent_unbound_responses_preserve_first_responder_wins(self):
        """Multiple concurrent handle_response tasks: exactly 1 accepted."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        hook_socket_server = MagicMock()
        hook_socket_server.respond_to_permission = AsyncMock()

        handler = UnboundPermissionHandler(
            bot=bot,
            hook_socket_server=hook_socket_server,
            allowed_user_ids={100, 200, 300},
            permission_ttl_sec=60,
            permission_callback_registry=_registry(),
        )

        event = HookEvent(
            session_id="sess-concurrent01",
            cwd="/tmp/proj",
            event="PermissionRequest",
            status="waiting_for_approval",
            tool="Bash",
            tool_use_id="tuid-concurrent01",
        )

        await handler.handle_unbound_permission(event)

        # Create a gate so all tasks attempt nearly simultaneously
        gate = asyncio.Event()

        async def respond(user_id: int, decision: str):
            await gate.wait()
            return await handler.handle_response(
                tool_use_id="tuid-concurrent01",
                user_id=user_id,
                decision=decision,
            )

        tasks = [
            asyncio.create_task(respond(100, "allow")),
            asyncio.create_task(respond(200, "deny")),
            asyncio.create_task(respond(300, "allow")),
        ]

        # Release all tasks at once
        gate.set()
        results = await asyncio.gather(*tasks)

        accepted_count = sum(1 for r in results if r.accepted is True)
        assert accepted_count == 1

        # respond_to_permission called exactly once
        hook_socket_server.respond_to_permission.assert_called_once()
