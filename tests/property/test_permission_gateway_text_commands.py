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
    ConsumeAlreadyResponded,
    ConsumeConsumed,
    ConsumeDispatchFailed,
    ConsumeNotFound,
    ConsumeUnauthorized,
    PermissionAction,
    PermissionCallbackRecordSnapshot,
    SessionOrigin,
)
from app.services.permission_gateway import BackendDispatchFailed, BackendDispatchSucceeded, BackendDispatchUnknown, PermissionGateway

TOKEN = "AbCd12_-"
USER_ID = 42
SESSION_ID = "session-1"
TOOL_USE_ID = "tool-1"


@dataclass
class TextRegistry:
    pending: bool
    consume_label: str
    transition_ok: bool = True

    def __post_init__(self) -> None:
        self.consumes: list[tuple[str, int, PermissionAction]] = []
        self.resolved: list[str] = []
        self.dispatch_failed: list[tuple[str, str]] = []

    async def find_pending_for_user(self, user_id: int, *, sort_desc_by_created_at: bool = True) -> list[PermissionCallbackRecordSnapshot]:
        assert user_id == USER_ID
        assert sort_desc_by_created_at is True
        return [_snapshot()] if self.pending else []

    async def consume(self, token: str, user_id: int, action: PermissionAction) -> object:
        self.consumes.append((token, user_id, action))
        if self.consume_label == "consumed":
            return ConsumeConsumed(_snapshot(action=action))
        if self.consume_label == "unauthorized":
            return ConsumeUnauthorized()
        if self.consume_label == "already":
            return ConsumeAlreadyResponded()
        if self.consume_label == "dispatch_unknown":
            return ConsumeDispatchFailed("dispatch_unknown")
        if self.consume_label == "dispatch_failed":
            return ConsumeDispatchFailed("backend_down")
        return ConsumeNotFound()

    async def mark_resolved(self, token: str) -> bool:
        self.resolved.append(token)
        return self.transition_ok

    async def mark_dispatch_failed(self, token: str, reason: str) -> bool:
        self.dispatch_failed.append((token, reason))
        return self.transition_ok


class DenyAutoApproveService(AutoApproveService):
    def __init__(self, deactivate_count: int) -> None:
        super().__init__()
        self.deactivate_count = deactivate_count
        self.deactivate_calls: list[int] = []

    async def deactivate_all_for_user(self, user_id: int) -> int:
        self.deactivate_calls.append(user_id)
        return self.deactivate_count


def _snapshot(*, action: PermissionAction | None = None) -> PermissionCallbackRecordSnapshot:
    now = datetime(2026, 5, 28, tzinfo=UTC)
    return PermissionCallbackRecordSnapshot(
        token=TOKEN,
        tool_use_id=TOOL_USE_ID,
        session_id=SESSION_ID,
        origin=SessionOrigin.OWNED,
        authorization_mode=AuthorizationMode.OWNER,
        authorized_user_ids=frozenset({USER_ID}),
        created_at=now,
        expires_at=now,
        status=CallbackRecordStatus.CLAIMED if action else CallbackRecordStatus.PENDING,
        decision=action,
        responded_by_user_id=USER_ID if action else None,
        responded_at=now if action else None,
        dispatch_error_reason=None,
    )


def _gateway(registry: TextRegistry, aas: AutoApproveService) -> PermissionGateway:
    return PermissionGateway(
        registry=registry,
        auto_approve_service=aas,
        task_service=SimpleNamespace(),
        hook_socket_server=SimpleNamespace(),
        unbound_responder=SimpleNamespace(),
        settings=SimpleNamespace(allow_all_users=False, allowed_user_id_set={USER_ID}),
        bot=SimpleNamespace(),
        message_builder=SimpleNamespace(),
    )


async def _dispatch(result: object, calls: list[PermissionAction], snapshot: object, action: PermissionAction) -> object:
    assert isinstance(snapshot, PermissionCallbackRecordSnapshot)
    calls.append(action)
    return result


@settings(max_examples=80, deadline=None)
@given(
    pending=st.booleans(),
    consume_label=st.sampled_from(["consumed", "unauthorized", "already", "dispatch_unknown", "dispatch_failed", "not_found"]),
    dispatch_label=st.sampled_from(["succeeded", "failed", "unknown"]),
    transition_ok=st.booleans(),
)
@pytest.mark.asyncio
async def test_approve_command_reply_mapping_is_deterministic(
    pending: bool,
    consume_label: str,
    dispatch_label: str,
    transition_ok: bool,
) -> None:
    registry = TextRegistry(pending=pending, consume_label=consume_label, transition_ok=transition_ok)
    gateway = _gateway(registry, AutoApproveService())
    dispatch_calls: list[PermissionAction] = []
    dispatch_result = {
        "succeeded": BackendDispatchSucceeded(),
        "failed": BackendDispatchFailed("backend_down"),
        "unknown": BackendDispatchUnknown("cancelled"),
    }[dispatch_label]
    gateway._dispatch_with_completion_tracking = lambda snapshot, action: _dispatch(  # type: ignore[method-assign]
        dispatch_result, dispatch_calls, snapshot, action
    )

    reply = await gateway.handle_approve_command(USER_ID)

    if not pending:
        assert reply == "当前没有待处理的权限请求"
        assert registry.consumes == []
        assert dispatch_calls == []
        return

    assert registry.consumes == [(TOKEN, USER_ID, PermissionAction.ALLOW)]
    if consume_label == "consumed":
        assert dispatch_calls == [PermissionAction.ALLOW]
        if dispatch_label == "succeeded":
            assert reply == ("已批准" if transition_ok else "会话已结束，按钮已失效")
        elif dispatch_label == "failed":
            assert reply == ("审批结果发送失败，请重新触发请求" if transition_ok else "会话已结束，按钮已失效")
        else:
            assert reply == (
                "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发"
                if transition_ok
                else "会话已结束；本次响应结果未知，后端可能已收到，请检查会话输出或重新触发"
            )
    else:
        assert dispatch_calls == []
        assert (
            reply
            == {
                "unauthorized": "当前没有待处理的权限请求",
                "already": "请求已被响应",
                "dispatch_unknown": "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发",
                "dispatch_failed": "审批结果发送失败，请重新触发请求",
                "not_found": "当前没有待处理的权限请求",
            }[consume_label]
        )


@settings(max_examples=80, deadline=None)
@given(
    auto_approve_was_active=st.booleans(),
    pending=st.booleans(),
    consume_label=st.sampled_from(["consumed", "unauthorized", "already", "dispatch_unknown", "dispatch_failed", "not_found"]),
    dispatch_label=st.sampled_from(["succeeded", "failed", "unknown"]),
    transition_ok=st.booleans(),
)
@pytest.mark.asyncio
async def test_deny_command_reply_mapping_is_deterministic(
    auto_approve_was_active: bool,
    pending: bool,
    consume_label: str,
    dispatch_label: str,
    transition_ok: bool,
) -> None:
    registry = TextRegistry(pending=pending, consume_label=consume_label, transition_ok=transition_ok)
    aas = DenyAutoApproveService(1 if auto_approve_was_active else 0)
    gateway = _gateway(registry, aas)
    dispatch_calls: list[PermissionAction] = []
    dispatch_result = {
        "succeeded": BackendDispatchSucceeded(),
        "failed": BackendDispatchFailed("backend_down"),
        "unknown": BackendDispatchUnknown("cancelled"),
    }[dispatch_label]
    gateway._dispatch_with_completion_tracking = lambda snapshot, action: _dispatch(  # type: ignore[method-assign]
        dispatch_result, dispatch_calls, snapshot, action
    )

    reply = await gateway.handle_deny_command(user_id=USER_ID, reason="no")

    prefix = "已关闭自动批准" if auto_approve_was_active else "自动批准未开启"
    assert aas.deactivate_calls == [USER_ID]
    if not pending:
        assert reply == f"{prefix}\n当前没有待处理的权限请求"
        assert registry.consumes == []
        assert dispatch_calls == []
        return

    assert registry.consumes == [(TOKEN, USER_ID, PermissionAction.DENY)]
    if consume_label == "consumed":
        assert dispatch_calls == [PermissionAction.DENY]
        if dispatch_label == "succeeded":
            outcome = "已拒绝" if transition_ok else "会话已结束，按钮已失效"
        elif dispatch_label == "failed":
            outcome = "审批结果发送失败，请重新触发请求" if transition_ok else "会话已结束，按钮已失效"
        else:
            outcome = (
                "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发"
                if transition_ok
                else "会话已结束；本次响应结果未知，后端可能已收到，请检查会话输出或重新触发"
            )
    else:
        assert dispatch_calls == []
        outcome = {
            "unauthorized": "当前没有待处理的权限请求",
            "already": "请求已被响应",
            "dispatch_unknown": "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发",
            "dispatch_failed": "审批结果发送失败，请重新触发请求",
            "not_found": "当前没有待处理的权限请求",
        }[consume_label]
    assert reply == f"{prefix}\n{outcome}"
