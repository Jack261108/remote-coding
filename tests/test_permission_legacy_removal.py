from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bootstrap_mixins import HookHandlingMixin
from app.domain.hook_models import HookEvent
from app.services.permission_callback_registry import AutoApproveOutcome, SessionOrigin


class _OwnershipResolver:
    async def resolve(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            ownership_state="bound",
            origin=SessionOrigin.EXTERNAL_BOUND,
            owner_user_id=42,
        )


class _Container(HookHandlingMixin):
    def __init__(self, cwd: str) -> None:
        self.settings = SimpleNamespace(allowed_workdirs=[cwd])
        self.ownership_resolver = _OwnershipResolver()
        self.auto_approve_service = SimpleNamespace(is_active=MagicMock(return_value=True))
        self.permission_gateway = SimpleNamespace(maybe_auto_approve=AsyncMock(return_value=AutoApproveOutcome.APPROVED))
        self.hook_socket_server = SimpleNamespace(respond_to_permission=AsyncMock(return_value=True))
        self.dispatched_events = []
        self.synced_sessions = []

    async def _dispatch_session_event(self, event) -> None:  # noqa: ANN001
        self.dispatched_events.append(event)

    def _schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
        self.synced_sessions.append((session_id, cwd))


def test_legacy_auto_approved_permission_helper_is_removed() -> None:
    assert not hasattr(HookHandlingMixin, "_handle_auto_approved_permission")


@pytest.mark.asyncio
async def test_auto_approve_hook_path_routes_through_gateway_without_direct_socket_response(tmp_path) -> None:
    container = _Container(str(tmp_path))
    event = HookEvent(
        session_id="legacy-auto-session",
        cwd=str(tmp_path),
        event="PermissionRequest",
        status="waiting_for_approval",
        tool="Bash",
        tool_input={"command": "pwd"},
        tool_use_id="legacy-auto-tool",
    )

    await container._handle_hook_event(event)

    container.permission_gateway.maybe_auto_approve.assert_awaited_once_with(
        session_id="legacy-auto-session",
        origin=SessionOrigin.EXTERNAL_BOUND,
        candidate_user_id=42,
        tool_use_id="legacy-auto-tool",
        tool_name="Bash",
        tool_input={"command": "pwd"},
    )
    container.hook_socket_server.respond_to_permission.assert_not_called()
