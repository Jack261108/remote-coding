from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.auto_approve_service import AutoApproveService
from app.services.permission_callback_registry import AuthorizationMode, PermissionCallbackRegistry, SessionOrigin
from app.services.permission_gateway import PermissionGateway, RegisterForButtonOk


@pytest.mark.asyncio
async def test_documented_gap_revoked_user_can_use_already_issued_unbound_button() -> None:
    user_a = 101
    user_b = 202
    settings = SimpleNamespace(allow_all_users=False, allowed_user_id_set={user_a, user_b})
    registry = PermissionCallbackRegistry(ttl_sec=600, token_factory=lambda: "tok12345")
    unbound_responder = SimpleNamespace(handle_response=AsyncMock(return_value=SimpleNamespace(accepted=True, forwarded=True)))
    gateway = PermissionGateway(
        registry=registry,
        auto_approve_service=AutoApproveService(),
        task_service=SimpleNamespace(),
        hook_socket_server=SimpleNamespace(),
        unbound_responder=unbound_responder,
        settings=settings,
        message_sender=SimpleNamespace(),
        message_builder=SimpleNamespace(),
    )

    result = await gateway.register_for_button(
        tool_use_id="tool-gap-1",
        session_id="session-gap-1",
        origin=SessionOrigin.EXTERNAL_UNBOUND,
        candidate_user_id=None,
    )
    assert isinstance(result, RegisterForButtonOk)
    record = registry._records["tok12345"]
    assert record.authorization_mode is AuthorizationMode.ALLOWED_USERS_SNAPSHOT
    assert record.authorized_user_ids == frozenset({user_a, user_b})

    # Documented gap: Requirement 6 AC12 re-validates current allowlist membership
    # only for future EXTERNAL_UNBOUND auto-approve interception. Per Requirement 9,
    # already-issued pending buttons keep their registry snapshot until expiry/session end.
    settings.allowed_user_id_set = {user_b}

    response = await gateway.handle_callback(data="perm:tok12345:allow", user_id=user_a)

    assert response.edit_message_text == "✅ 用户已批准"
    unbound_responder.handle_response.assert_awaited_once_with(
        tool_use_id="tool-gap-1",
        user_id=user_a,
        decision="allow",
    )
