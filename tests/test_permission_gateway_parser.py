from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.auto_approve_service import AutoApproveService
from app.services.permission_callback_registry import (
    AuthorizationMode,
    CallbackRecordStatus,
    ConsumeConsumed,
    PermissionAction,
    PermissionCallbackRecordSnapshot,
    SessionOrigin,
)
from app.services.permission_gateway import BackendDispatchSucceeded, PermissionGateway

TOKEN = "AbCd12_-"
USER_ID = 42
SESSION_ID = "session-1"
TOOL_USE_ID = "tool-1"


@dataclass
class ParserRegistry:
    def __post_init__(self) -> None:
        self.consumes: list[tuple[str, int, PermissionAction]] = []
        self.resolved: list[str] = []

    async def consume(self, token: str, user_id: int, action: PermissionAction) -> object:
        self.consumes.append((token, user_id, action))
        return ConsumeConsumed(_snapshot(token=token, action=action))

    async def mark_resolved(self, token: str) -> bool:
        self.resolved.append(token)
        return True


def _snapshot(*, token: str, action: PermissionAction) -> PermissionCallbackRecordSnapshot:
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
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
        bot=SimpleNamespace(),
        message_builder=SimpleNamespace(),
    )
    gateway._dispatch_with_completion_tracking = lambda snapshot, action: _dispatch(snapshot, action)  # type: ignore[method-assign]
    return gateway


async def _dispatch(snapshot: object, action: PermissionAction) -> object:
    assert isinstance(snapshot, PermissionCallbackRecordSnapshot)
    assert snapshot.decision is action
    return BackendDispatchSucceeded()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected_token"),
    [
        (f"perm:{TOKEN}:allow", TOKEN),
        (f"perm:allow:{TOKEN}", TOKEN),
        (f"ext_perm:{TOKEN}:allow", TOKEN),
        ("perm:short:allow", "short"),
    ],
)
async def test_all_callback_shapes_route_through_same_dispatch_table(data: str, expected_token: str) -> None:
    registry = ParserRegistry()
    gateway = _gateway(registry)

    response = await gateway.handle_callback(data=data, user_id=USER_ID)

    assert response.alert_text == "已批准"
    assert registry.consumes == [(expected_token, USER_ID, PermissionAction.ALLOW)]
    assert registry.resolved == [expected_token]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "perm:bad.token:allow",
        f"perm:{TOKEN}:approve",
        "perm:allow",
        "ext_perm:token-only",
        "other:payload",
    ],
)
async def test_malformed_callback_payload_returns_rejection_alert(data: str) -> None:
    registry = ParserRegistry()
    gateway = _gateway(registry)

    response = await gateway.handle_callback(data=data, user_id=USER_ID)

    assert response.alert_text == "无效的权限响应"
    assert registry.consumes == []
    assert registry.resolved == []
