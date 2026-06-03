from __future__ import annotations

from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.auto_approve_service import AutoApproveService
from app.services.permission_callback_registry import AutoApproveOutcome, PermissionAction, SessionOrigin
from app.services.permission_gateway import BackendDispatchFailed, BackendDispatchSucceeded, BackendDispatchUnknown, PermissionGateway

USER_ID = 42
SESSION_ID = "session-1"
TOOL_USE_ID = "tool-1"


class MaybeAutoApproveService(AutoApproveService):
    def __init__(self, *, active: bool) -> None:
        super().__init__()
        self.active = active
        self.deactivated: list[tuple[int, str]] = []

    def is_active(self, session_id: str | None = None, *, user_id: int | None = None) -> bool:
        assert session_id == SESSION_ID
        assert user_id == USER_ID
        return self.active

    async def deactivate_and_release_for_user_session(self, *, user_id: int, session_id: str) -> bool:
        self.deactivated.append((user_id, session_id))
        self.active = False
        return True


class RecordingMessageSender:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, *, keyboard=None, parse_mode=None) -> None:
        self.messages.append((chat_id, text))


def _gateway(*, aas: MaybeAutoApproveService, allowed_ids: set[int], message_sender: RecordingMessageSender) -> PermissionGateway:
    return PermissionGateway(
        registry=SimpleNamespace(),
        auto_approve_service=aas,
        task_service=SimpleNamespace(),
        hook_socket_server=SimpleNamespace(),
        unbound_responder=SimpleNamespace(),
        settings=SimpleNamespace(allow_all_users=False, allowed_user_id_set=allowed_ids),
        message_sender=message_sender,
        message_builder=SimpleNamespace(),
    )


async def _dispatch(result: object, calls: list[tuple[str, PermissionAction]], snapshot: object, action: PermissionAction) -> object:
    calls.append((snapshot.tool_use_id, action))
    assert snapshot.token.startswith("auto:")
    assert snapshot.responded_by_user_id == USER_ID
    assert snapshot.responded_at is not None
    return result


@settings(max_examples=80, deadline=None)
@given(
    origin=st.sampled_from(list(SessionOrigin)),
    candidate_present=st.booleans(),
    active=st.booleans(),
    user_allowed=st.booleans(),
    dispatch_label=st.sampled_from(["succeeded", "failed", "unknown"]),
)
@pytest.mark.asyncio
async def test_maybe_auto_approve_outcome_and_side_effects_are_deterministic(
    origin: SessionOrigin,
    candidate_present: bool,
    active: bool,
    user_allowed: bool,
    dispatch_label: str,
) -> None:
    aas = MaybeAutoApproveService(active=active)
    sender = RecordingMessageSender()
    gateway = _gateway(aas=aas, allowed_ids={USER_ID} if user_allowed else set(), message_sender=sender)
    dispatch_calls: list[tuple[str, PermissionAction]] = []
    dispatch_result = {
        "succeeded": BackendDispatchSucceeded(),
        "failed": BackendDispatchFailed("backend_down"),
        "unknown": BackendDispatchUnknown("cancelled"),
    }[dispatch_label]
    gateway._dispatch_with_completion_tracking = lambda snapshot, action: _dispatch(  # type: ignore[method-assign]
        dispatch_result, dispatch_calls, snapshot, action
    )

    outcome = await gateway.maybe_auto_approve(
        session_id=SESSION_ID,
        origin=origin,
        candidate_user_id=USER_ID if candidate_present else None,
        tool_use_id=TOOL_USE_ID,
        tool_name="Bash",
        tool_input={"command": "pwd"},
    )

    if not candidate_present or not active:
        assert outcome is AutoApproveOutcome.NOT_APPROVED
        assert dispatch_calls == []
        assert aas.deactivated == []
        assert sender.messages == []
    elif origin is SessionOrigin.EXTERNAL_UNBOUND and not user_allowed:
        assert outcome is AutoApproveOutcome.NOT_APPROVED
        assert dispatch_calls == []
        assert aas.deactivated == [(USER_ID, SESSION_ID)]
        assert sender.messages == []
    elif dispatch_label == "succeeded":
        assert outcome is AutoApproveOutcome.APPROVED
        assert dispatch_calls == [(TOOL_USE_ID, PermissionAction.AUTO_APPROVE)]
        assert aas.deactivated == []
        assert sender.messages == []
    elif dispatch_label == "failed":
        assert outcome is AutoApproveOutcome.APPROVAL_FAILED
        assert dispatch_calls == [(TOOL_USE_ID, PermissionAction.AUTO_APPROVE)]
        assert aas.deactivated == []
        assert sender.messages == []
    else:
        assert outcome is AutoApproveOutcome.APPROVAL_UNKNOWN
        assert dispatch_calls == [(TOOL_USE_ID, PermissionAction.AUTO_APPROVE)]
        assert aas.deactivated == []
        assert sender.messages == [(USER_ID, "本次请求的自动批准结果未知，请检查最近的操作是否已生效；如未生效请重新触发")]
