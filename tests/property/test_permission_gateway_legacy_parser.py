from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.auto_approve_service import AutoApproveService
from app.services.permission_callback_registry import (
    AuthorizationMode,
    CallbackRecordStatus,
    ConsumeConsumed,
    ConsumeNotFound,
    PermissionAction,
    PermissionCallbackRecordSnapshot,
    SessionOrigin,
)
from app.services.permission_gateway import BackendDispatchSucceeded, PermissionGateway

USER_ID = 42
SESSION_ID = "session-1"
TOOL_USE_ID = "tool-1"


@dataclass
class ParserRegistry:
    known_token: str
    new_registry_has_token: bool = True

    def __post_init__(self) -> None:
        self.consumes: list[tuple[str, int, PermissionAction]] = []
        self.resolved: list[str] = []

    async def consume(self, token: str, user_id: int, action: PermissionAction) -> object:
        self.consumes.append((token, user_id, action))
        if token != self.known_token or not self.new_registry_has_token:
            return ConsumeNotFound()
        return ConsumeConsumed(_snapshot(token=token, action=action))

    async def mark_resolved(self, token: str) -> bool:
        self.resolved.append(token)
        return True


def _snapshot(*, token: str, action: PermissionAction) -> PermissionCallbackRecordSnapshot:
    now = datetime(2026, 5, 28, tzinfo=UTC)
    return PermissionCallbackRecordSnapshot(
        token=token,
        tool_use_id=TOOL_USE_ID,
        session_id=SESSION_ID,
        origin=SessionOrigin.OWNED,
        authorization_mode=AuthorizationMode.OWNER,
        authorized_user_ids=frozenset({USER_ID}),
        created_at=now,
        expires_at=now,
        status=CallbackRecordStatus.CLAIMED,
        decision=action,
        responded_by_user_id=USER_ID,
        responded_at=now,
        dispatch_error_reason=None,
    )


def _gateway(registry: ParserRegistry) -> PermissionGateway:
    gateway = PermissionGateway(
        registry=registry,
        auto_approve_service=AutoApproveService(),
        task_service=SimpleNamespace(),
        hook_socket_server=SimpleNamespace(),
        unbound_responder=SimpleNamespace(),
        settings=SimpleNamespace(allow_all_users=False, allowed_user_id_set={USER_ID}),
        message_sender=SimpleNamespace(),
        message_builder=SimpleNamespace(),
    )
    gateway._dispatch_with_completion_tracking = lambda snapshot, action: _dispatch(snapshot, action)  # type: ignore[method-assign]
    return gateway


async def _dispatch(snapshot: object, action: PermissionAction) -> object:
    assert isinstance(snapshot, PermissionCallbackRecordSnapshot)
    assert snapshot.decision is action
    return BackendDispatchSucceeded()


@settings(max_examples=60, deadline=None)
@given(
    token=st.from_regex(r"[A-Za-z0-9_-]{8}", fullmatch=True),
    action=st.sampled_from([PermissionAction.ALLOW, PermissionAction.DENY]),
    legacy_kind=st.sampled_from(["owned", "external"]),
)
@pytest.mark.asyncio
async def test_legacy_callback_shapes_reformat_to_the_same_dispatch_path(
    token: str,
    action: PermissionAction,
    legacy_kind: str,
) -> None:
    registry = ParserRegistry(known_token=token)
    gateway = _gateway(registry)
    data = f"perm:{action.value}:{token}" if legacy_kind == "owned" else f"ext_perm:{token}:{action.value}"

    response = await gateway.handle_callback(data=data, user_id=USER_ID)

    expected = "✅ 用户已批准" if action is PermissionAction.ALLOW else "❌ 用户已拒绝"
    assert response.edit_message_text == expected
    assert registry.consumes == [(token, USER_ID, action)]
    assert registry.resolved == [token]


@pytest.mark.asyncio
async def test_legacy_callback_token_missing_from_new_registry_returns_expired_alert() -> None:
    registry = ParserRegistry(known_token="legacy01", new_registry_has_token=False)
    gateway = _gateway(registry)

    response = await gateway.handle_callback(data="ext_perm:legacy01:allow", user_id=USER_ID)

    assert response.edit_message_text == "按钮已过期，请重新触发请求"
    assert registry.consumes == [("legacy01", USER_ID, PermissionAction.ALLOW)]
    assert registry.resolved == []
