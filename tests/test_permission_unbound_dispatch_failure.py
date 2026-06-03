from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.presenters.permission_message_builder import PermissionMessageBuilder
from app.services.auto_approve_service import AutoApproveService
from app.services.permission_callback_registry import (
    AuthorizationMode,
    CallbackRecordStatus,
    ConsumeConsumed,
    PermissionAction,
    PermissionCallbackRegistry,
    SessionOrigin,
)
from app.services.permission_gateway import BackendDispatchFailed, PermissionGateway
from app.services.unbound_permission_handler import UnboundPermissionResponseResult


class _UnboundResponder:
    async def handle_response(self, *, tool_use_id: str, user_id: int, decision: str) -> UnboundPermissionResponseResult:
        assert tool_use_id == "tool-1"
        assert user_id == 42
        assert decision == "allow"
        return UnboundPermissionResponseResult(accepted=True, forwarded=False)


@pytest.mark.asyncio
async def test_unbound_dispatch_failure_transitions_record_to_dispatch_failed() -> None:
    registry = PermissionCallbackRegistry(ttl_sec=60)
    token = await registry.register_token(
        tool_use_id="tool-1",
        session_id="session-1",
        origin=SessionOrigin.EXTERNAL_UNBOUND,
        authorization_mode=AuthorizationMode.ALLOWED_USERS_SNAPSHOT,
        authorized_user_ids=frozenset({42}),
    )
    consume_result = await registry.consume(token, 42, PermissionAction.AUTO_APPROVE)
    assert isinstance(consume_result, ConsumeConsumed)

    gateway = PermissionGateway(
        registry=registry,
        auto_approve_service=AutoApproveService(),
        task_service=SimpleNamespace(),
        hook_socket_server=SimpleNamespace(),
        unbound_responder=_UnboundResponder(),
        settings=SimpleNamespace(allow_all_users=False, allowed_user_id_set={42}),
        message_sender=SimpleNamespace(),
        message_builder=PermissionMessageBuilder(),
    )

    dispatch_result = await gateway._dispatch_with_completion_tracking(consume_result.snapshot, PermissionAction.AUTO_APPROVE)

    assert dispatch_result == BackendDispatchFailed("unbound_not_forwarded")
    assert await registry.mark_dispatch_failed(token, dispatch_result.reason) is True
    record = registry._records[token]
    assert record.status is CallbackRecordStatus.DISPATCH_FAILED
    assert record.dispatch_error_reason == "unbound_not_forwarded"
