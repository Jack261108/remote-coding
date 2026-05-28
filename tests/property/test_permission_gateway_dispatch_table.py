from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.auto_approve_service import AutoApproveService, SlotClaimed
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
    PreflightEligible,
    PreflightNotUnbound,
    SessionOrigin,
)
from app.services.permission_gateway import BackendDispatchFailed, BackendDispatchSucceeded, BackendDispatchUnknown, PermissionGateway
from app.services.unbound_permission_handler import UnboundPermissionResponseResult

TOKEN = "AbCd12_-"
USER_ID = 42
SESSION_ID = "session-1"
TOOL_USE_ID = "tool-1"


@dataclass
class RecordingRegistry:
    consume_result_label: str
    transition_ok: bool = True
    preflight_result: object | None = None

    def __post_init__(self) -> None:
        self.consumes: list[tuple[str, int, PermissionAction]] = []
        self.resolved: list[str] = []
        self.dispatch_failed: list[tuple[str, str]] = []

    async def consume(self, token: str, user_id: int, action: PermissionAction) -> object:
        self.consumes.append((token, user_id, action))
        if self.consume_result_label == "consumed":
            return ConsumeConsumed(_snapshot(action=action))
        if self.consume_result_label == "unauthorized":
            return ConsumeUnauthorized()
        if self.consume_result_label == "already":
            return ConsumeAlreadyResponded()
        if self.consume_result_label == "dispatch_unknown":
            return ConsumeDispatchFailed("dispatch_unknown")
        if self.consume_result_label == "dispatch_failed":
            return ConsumeDispatchFailed("backend_down")
        return ConsumeNotFound()

    async def inspect_for_auto_approve_preflight(self, token: str, user_id: int) -> object:
        assert token == TOKEN
        assert user_id == USER_ID
        return self.preflight_result if self.preflight_result is not None else PreflightNotUnbound(_snapshot(action=None))

    async def mark_resolved(self, token: str) -> bool:
        self.resolved.append(token)
        return self.transition_ok

    async def mark_dispatch_failed(self, token: str, reason: str) -> bool:
        self.dispatch_failed.append((token, reason))
        return self.transition_ok


class RecordingAutoApproveService(AutoApproveService):
    def __init__(self) -> None:
        super().__init__()
        self.released: list[tuple[str, int, str]] = []
        self.committed: list[tuple[str, int, str]] = []

    async def release_slot(self, *, session_id: str, user_id: int, attempt_id: str) -> bool:
        self.released.append((session_id, user_id, attempt_id))
        return await super().release_slot(session_id=session_id, user_id=user_id, attempt_id=attempt_id)

    async def commit_slot_if_session_alive(self, *, session_id: str, user_id: int, attempt_id: str) -> object:
        self.committed.append((session_id, user_id, attempt_id))
        return await super().commit_slot_if_session_alive(session_id=session_id, user_id=user_id, attempt_id=attempt_id)


def _snapshot(*, action: PermissionAction | None, origin: SessionOrigin = SessionOrigin.OWNED) -> PermissionCallbackRecordSnapshot:
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    return PermissionCallbackRecordSnapshot(
        token=TOKEN,
        tool_use_id=TOOL_USE_ID,
        session_id=SESSION_ID,
        origin=origin,
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


def _gateway(registry: RecordingRegistry, aas: AutoApproveService) -> PermissionGateway:
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


class CancellingConsumeRegistry(RecordingRegistry):
    async def consume(self, token: str, user_id: int, action: PermissionAction) -> object:
        self.consumes.append((token, user_id, action))
        raise asyncio.CancelledError


class RaisingMarkResolvedRegistry(RecordingRegistry):
    async def mark_resolved(self, token: str) -> bool:
        self.resolved.append(token)
        raise RuntimeError("mark failed")


@pytest.mark.asyncio
async def test_unbound_auto_approve_releases_slot_when_cancelled_after_claim() -> None:
    aas = RecordingAutoApproveService()
    snapshot = _snapshot(action=None, origin=SessionOrigin.EXTERNAL_UNBOUND)
    registry = CancellingConsumeRegistry("consumed", preflight_result=PreflightEligible(snapshot))
    gateway = _gateway(registry, aas)

    with pytest.raises(asyncio.CancelledError):
        await gateway.handle_callback(data=f"perm:{TOKEN}:auto_approve", user_id=USER_ID)

    assert len(aas.released) == 1
    reclaimed = await aas.try_claim_slot(session_id=SESSION_ID, user_id=USER_ID)
    assert isinstance(reclaimed, SlotClaimed)


@pytest.mark.asyncio
async def test_unbound_auto_approve_releases_slot_when_mark_resolved_raises_after_claim() -> None:
    aas = RecordingAutoApproveService()
    snapshot = _snapshot(action=None, origin=SessionOrigin.EXTERNAL_UNBOUND)
    registry = RaisingMarkResolvedRegistry("consumed", preflight_result=PreflightEligible(snapshot))
    gateway = _gateway(registry, aas)
    dispatch_calls: list[PermissionAction] = []
    gateway._dispatch_with_completion_tracking = lambda snap, sent_action: _dispatch(  # type: ignore[method-assign]
        BackendDispatchSucceeded(), dispatch_calls, snap, sent_action
    )

    with pytest.raises(RuntimeError, match="mark failed"):
        await gateway.handle_callback(data=f"perm:{TOKEN}:auto_approve", user_id=USER_ID)

    assert dispatch_calls == [PermissionAction.AUTO_APPROVE]
    assert len(aas.released) == 1
    reclaimed = await aas.try_claim_slot(session_id=SESSION_ID, user_id=USER_ID)
    assert isinstance(reclaimed, SlotClaimed)


@settings(max_examples=80, deadline=None)
@given(
    consume_label=st.sampled_from(["consumed", "unauthorized", "already", "dispatch_unknown", "dispatch_failed", "not_found"]),
    action=st.sampled_from([PermissionAction.ALLOW, PermissionAction.DENY]),
    dispatch_label=st.sampled_from(["succeeded", "failed", "unknown"]),
    transition_ok=st.booleans(),
)
@pytest.mark.asyncio
async def test_callback_dispatch_table_for_allow_and_deny_is_deterministic(
    consume_label: str,
    action: PermissionAction,
    dispatch_label: str,
    transition_ok: bool,
) -> None:
    registry = RecordingRegistry(consume_label, transition_ok=transition_ok)
    gateway = _gateway(registry, AutoApproveService())
    dispatch_calls: list[PermissionAction] = []
    dispatch_result = {
        "succeeded": BackendDispatchSucceeded(),
        "failed": BackendDispatchFailed("backend_down"),
        "unknown": BackendDispatchUnknown("cancelled"),
    }[dispatch_label]
    gateway._dispatch_with_completion_tracking = lambda snapshot, sent_action: _dispatch(  # type: ignore[method-assign]
        dispatch_result, dispatch_calls, snapshot, sent_action
    )

    response = await gateway.handle_callback(data=f"perm:{TOKEN}:{action.value}", user_id=USER_ID)

    assert registry.consumes == [(TOKEN, USER_ID, action)]
    if consume_label == "consumed":
        assert dispatch_calls == [action]
        if dispatch_label == "succeeded":
            assert registry.resolved == [TOKEN]
            assert registry.dispatch_failed == []
            expected = "已批准" if action is PermissionAction.ALLOW else "已拒绝"
            assert response.alert_text == (expected if transition_ok else "会话已结束，按钮已失效")
        elif dispatch_label == "failed":
            assert registry.resolved == []
            assert registry.dispatch_failed == [(TOKEN, "backend_down")]
            assert response.alert_text == ("上次发送审批结果失败，请重新触发请求" if transition_ok else "会话已结束，按钮已失效")
        else:
            assert registry.resolved == []
            assert registry.dispatch_failed == [(TOKEN, "dispatch_unknown")]
            assert response.alert_text == (
                "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发"
                if transition_ok
                else "会话已结束；本次响应结果未知，后端可能已收到，请检查会话输出或重新触发"
            )
    else:
        assert dispatch_calls == []
        assert registry.resolved == []
        assert registry.dispatch_failed == []
        assert (
            response.alert_text
            == {
                "unauthorized": "无权限响应此请求",
                "already": "已响应过",
                "dispatch_unknown": "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发",
                "dispatch_failed": "上次发送审批结果失败，请重新触发请求",
                "not_found": "按钮已过期，请重新触发请求",
            }[consume_label]
        )


@pytest.mark.asyncio
async def test_unbound_auto_approve_claims_slot_and_commits_after_successful_dispatch() -> None:
    aas = RecordingAutoApproveService()
    claimed = await aas.try_claim_slot(session_id=SESSION_ID, user_id=USER_ID)
    assert isinstance(claimed, SlotClaimed)
    await aas.release_slot(session_id=SESSION_ID, user_id=USER_ID, attempt_id=claimed.attempt_id)

    snapshot = _snapshot(action=None, origin=SessionOrigin.EXTERNAL_UNBOUND)
    registry = RecordingRegistry("consumed", preflight_result=PreflightEligible(snapshot))
    gateway = _gateway(registry, aas)
    dispatch_calls: list[PermissionAction] = []
    gateway._dispatch_with_completion_tracking = lambda snap, sent_action: _dispatch(  # type: ignore[method-assign]
        BackendDispatchSucceeded(), dispatch_calls, snap, sent_action
    )

    response = await gateway.handle_callback(data=f"perm:{TOKEN}:auto_approve", user_id=USER_ID)

    assert response.alert_text == "已开启自动批准"
    assert dispatch_calls == [PermissionAction.AUTO_APPROVE]
    assert registry.resolved == [TOKEN]
    assert registry.dispatch_failed == []
    assert aas.get_active_user_for_session(SESSION_ID) == USER_ID


@pytest.mark.asyncio
async def test_unbound_auto_approve_slot_conflict_leaves_record_pending() -> None:
    aas = AutoApproveService()
    await aas.try_claim_slot(session_id=SESSION_ID, user_id=99)
    snapshot = _snapshot(action=None, origin=SessionOrigin.EXTERNAL_UNBOUND)
    registry = RecordingRegistry("consumed", preflight_result=PreflightEligible(snapshot))
    gateway = _gateway(registry, aas)

    response = await gateway.handle_callback(data=f"perm:{TOKEN}:auto_approve", user_id=USER_ID)

    assert response.alert_text == "已被其他用户激活"
    assert registry.consumes == []
    assert registry.resolved == []
    assert registry.dispatch_failed == []


class KwargsTaskService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def respond_to_pending_permission(self, **kwargs: object) -> tuple[bool, str]:
        self.calls.append(kwargs)
        return True, "ok"


class UnboundResponder:
    def __init__(self, result: UnboundPermissionResponseResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def handle_response(self, **kwargs: object) -> UnboundPermissionResponseResult:
        self.calls.append(kwargs)
        return self.result


class CancellingHookSocketServer:
    async def respond_to_permission(self, **kwargs: object) -> bool:
        del kwargs
        raise asyncio.CancelledError


def _dispatch_gateway(*, task_service: object, hook_socket_server: object, unbound_responder: object) -> PermissionGateway:
    return PermissionGateway(
        registry=SimpleNamespace(),
        auto_approve_service=AutoApproveService(),
        task_service=task_service,
        hook_socket_server=hook_socket_server,
        unbound_responder=unbound_responder,
        settings=SimpleNamespace(allow_all_users=False, allowed_user_id_set={USER_ID}),
        bot=SimpleNamespace(),
        message_builder=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_dispatch_owned_passes_expected_tool_use_id_when_kwargs_supported() -> None:
    task_service = KwargsTaskService()
    gateway = _dispatch_gateway(
        task_service=task_service,
        hook_socket_server=SimpleNamespace(),
        unbound_responder=SimpleNamespace(),
    )

    result = await gateway._dispatch_with_completion_tracking(_snapshot(action=PermissionAction.ALLOW), PermissionAction.AUTO_APPROVE)

    assert isinstance(result, BackendDispatchSucceeded)
    assert task_service.calls == [
        {
            "user_id": USER_ID,
            "decision": "allow",
            "reason": f"responded by user {USER_ID}",
            "expected_tool_use_id": TOOL_USE_ID,
        }
    ]


@pytest.mark.asyncio
async def test_dispatch_unbound_requires_accepted_and_forwarded() -> None:
    responder = UnboundResponder(UnboundPermissionResponseResult(accepted=True, forwarded=False))
    gateway = _dispatch_gateway(
        task_service=SimpleNamespace(),
        hook_socket_server=SimpleNamespace(),
        unbound_responder=responder,
    )

    result = await gateway._dispatch_with_completion_tracking(
        _snapshot(action=PermissionAction.ALLOW, origin=SessionOrigin.EXTERNAL_UNBOUND),
        PermissionAction.ALLOW,
    )

    assert result == BackendDispatchFailed("unbound_not_forwarded")
    assert responder.calls == [{"tool_use_id": TOOL_USE_ID, "user_id": USER_ID, "decision": "allow"}]


@pytest.mark.asyncio
async def test_dispatch_cancelled_after_handoff_is_unknown() -> None:
    gateway = _dispatch_gateway(
        task_service=SimpleNamespace(),
        hook_socket_server=CancellingHookSocketServer(),
        unbound_responder=SimpleNamespace(),
    )

    result = await gateway._dispatch_with_completion_tracking(
        _snapshot(action=PermissionAction.DENY, origin=SessionOrigin.EXTERNAL_BOUND),
        PermissionAction.DENY,
    )

    assert result == BackendDispatchUnknown("cancelled")
