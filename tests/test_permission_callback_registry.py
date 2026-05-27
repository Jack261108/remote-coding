from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import get_args

import pytest

from app.bot.presenters.permission_message_builder import PermissionPromptInput
from app.services.auto_approve_service import (
    ActivationSlot,
    AutoApproveActivation,
    CommitSlotMismatch,
    CommitSlotResult,
    CommitSlotSessionEnded,
    CommitSlotSucceeded,
    SlotActiveOwnerExists,
    SlotAlreadyClaimedBySameUser,
    SlotClaimResult,
    SlotClaimed,
    SlotConflict,
)
from app.services.permission_callback_registry import (
    AuthorizationMode,
    AutoApproveOutcome,
    CallbackRecordStatus,
    ConsumeAlreadyResponded,
    ConsumeConsumed,
    ConsumeDispatchFailed,
    ConsumeNotFound,
    ConsumeResult,
    ConsumeUnauthorized,
    InFlightConflictError,
    PermissionAction,
    PermissionCallbackRecord,
    PermissionCallbackRecordSnapshot,
    PermissionCallbackRegistry,
    PreflightAlreadyResponded,
    PreflightDispatchFailed,
    PreflightEligible,
    PreflightNotFound,
    PreflightNotUnbound,
    PreflightResult,
    PreflightUnauthorized,
    SessionOrigin,
)
from app.services.permission_gateway import (
    BackendDispatchFailed,
    BackendDispatchResult,
    BackendDispatchSucceeded,
    BackendDispatchUnknown,
    CallbackResponse,
)


def test_phase1_permission_callback_enums_use_explicit_string_values() -> None:
    assert SessionOrigin.OWNED == "owned"
    assert SessionOrigin.EXTERNAL_BOUND == "external_bound"
    assert SessionOrigin.EXTERNAL_UNBOUND == "external_unbound"
    assert AuthorizationMode.OWNER == "owner"
    assert AuthorizationMode.BOUND_USER == "bound_user"
    assert AuthorizationMode.ALLOWED_USERS_SNAPSHOT == "allowed_users_snapshot"
    assert AuthorizationMode.ALL_USERS == "all_users"
    assert AuthorizationMode.SOLE_AUTO_APPROVE_USER == "sole_auto_approve_user"
    assert PermissionAction.ALLOW == "allow"
    assert PermissionAction.DENY == "deny"
    assert PermissionAction.AUTO_APPROVE == "auto_approve"
    assert CallbackRecordStatus.PENDING == "pending"
    assert CallbackRecordStatus.CLAIMED == "claimed"
    assert CallbackRecordStatus.RESOLVED == "resolved"
    assert CallbackRecordStatus.DISPATCH_FAILED == "dispatch_failed"
    assert CallbackRecordStatus.SESSION_ENDED == "session_ended"
    assert CallbackRecordStatus.SUPERSEDED == "superseded"
    assert AutoApproveOutcome.APPROVED == "approved"
    assert AutoApproveOutcome.NOT_APPROVED == "not_approved"
    assert AutoApproveOutcome.APPROVAL_FAILED == "approval_failed"
    assert AutoApproveOutcome.APPROVAL_UNKNOWN == "approval_unknown"


def test_phase1_permission_callback_snapshot_copies_mutable_record() -> None:
    created_at = datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc)
    expires_at = datetime(2026, 5, 27, 10, 5, tzinfo=timezone.utc)
    record = PermissionCallbackRecord(
        token="tok12345",
        tool_use_id="toolu_abc",
        session_id="sess-123",
        origin=SessionOrigin.EXTERNAL_UNBOUND,
        authorization_mode=AuthorizationMode.ALL_USERS,
        authorized_user_ids=frozenset({42, 100}),
        created_at=created_at,
        expires_at=expires_at,
        status=CallbackRecordStatus.PENDING,
        decision=None,
        responded_by_user_id=None,
        responded_at=None,
        dispatch_error_reason=None,
    )

    snapshot = PermissionCallbackRecordSnapshot.from_record(record)
    record.status = CallbackRecordStatus.RESOLVED
    record.decision = PermissionAction.ALLOW

    assert snapshot.token == "tok12345"
    assert snapshot.authorized_user_ids == frozenset({42, 100})
    assert snapshot.status is CallbackRecordStatus.PENDING
    assert snapshot.decision is None
    with pytest.raises(FrozenInstanceError):
        snapshot.status = CallbackRecordStatus.CLAIMED  # type: ignore[misc]


def test_phase1_permission_callback_result_variants_are_available() -> None:
    snapshot = PermissionCallbackRecordSnapshot(
        token="tok12345",
        tool_use_id="toolu_abc",
        session_id="sess-123",
        origin=SessionOrigin.OWNED,
        authorization_mode=AuthorizationMode.OWNER,
        authorized_user_ids=frozenset({42}),
        created_at=datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 5, 27, 10, 5, tzinfo=timezone.utc),
        status=CallbackRecordStatus.PENDING,
        decision=None,
        responded_by_user_id=None,
        responded_at=None,
        dispatch_error_reason=None,
    )

    assert set(get_args(ConsumeResult)) == {
        ConsumeConsumed,
        ConsumeUnauthorized,
        ConsumeAlreadyResponded,
        ConsumeDispatchFailed,
        ConsumeNotFound,
    }
    assert set(get_args(PreflightResult)) == {
        PreflightEligible,
        PreflightNotUnbound,
        PreflightUnauthorized,
        PreflightAlreadyResponded,
        PreflightDispatchFailed,
        PreflightNotFound,
    }
    assert ConsumeConsumed(snapshot).snapshot is snapshot
    assert isinstance(ConsumeUnauthorized(), ConsumeUnauthorized)
    assert isinstance(ConsumeAlreadyResponded(), ConsumeAlreadyResponded)
    assert ConsumeDispatchFailed("backend unavailable").reason == "backend unavailable"
    assert isinstance(ConsumeNotFound(), ConsumeNotFound)
    assert PreflightEligible(snapshot).snapshot is snapshot
    assert PreflightNotUnbound(snapshot).snapshot is snapshot
    assert isinstance(PreflightUnauthorized(), PreflightUnauthorized)
    assert isinstance(PreflightAlreadyResponded(), PreflightAlreadyResponded)
    assert PreflightDispatchFailed("dispatch failed").reason == "dispatch failed"
    assert isinstance(PreflightNotFound(), PreflightNotFound)
    assert issubclass(InFlightConflictError, Exception)


def test_phase1_permission_gateway_models_are_available() -> None:
    assert set(get_args(BackendDispatchResult)) == {
        BackendDispatchSucceeded,
        BackendDispatchFailed,
        BackendDispatchUnknown,
    }
    assert isinstance(BackendDispatchSucceeded(), BackendDispatchSucceeded)
    assert BackendDispatchFailed("not connected").reason == "not connected"
    assert BackendDispatchUnknown("timeout").reason == "timeout"
    response = CallbackResponse(
        alert_text="已允许",
        show_alert=True,
        edit_message_text="权限已处理",
        clear_keyboard=True,
    )

    assert response.alert_text == "已允许"
    assert response.show_alert is True
    with pytest.raises(FrozenInstanceError):
        response.show_alert = False  # type: ignore[misc]


def test_phase1_auto_approve_slot_models_are_available() -> None:
    activated_at = datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc)
    activation = AutoApproveActivation("sess-123", 42, activated_at)
    slot = ActivationSlot("sess-123", 42, "attempt-1")

    assert set(get_args(SlotClaimResult)) == {
        SlotClaimed,
        SlotConflict,
        SlotAlreadyClaimedBySameUser,
        SlotActiveOwnerExists,
    }
    assert set(get_args(CommitSlotResult)) == {
        CommitSlotSucceeded,
        CommitSlotSessionEnded,
        CommitSlotMismatch,
    }
    assert activation.activated_at is activated_at
    assert slot.attempt_id == "attempt-1"
    assert SlotClaimed("attempt-1").attempt_id == "attempt-1"
    assert SlotConflict(42).holder_user_id == 42
    assert SlotAlreadyClaimedBySameUser("attempt-1").attempt_id == "attempt-1"
    assert SlotActiveOwnerExists(42).owner_user_id == 42
    assert isinstance(CommitSlotSucceeded(), CommitSlotSucceeded)
    assert isinstance(CommitSlotSessionEnded(), CommitSlotSessionEnded)
    assert isinstance(CommitSlotMismatch(), CommitSlotMismatch)


def test_phase1_permission_prompt_input_is_available() -> None:
    prompt_input = PermissionPromptInput(
        tool_name="Edit",
        tool_input={"file_path": "/x.py"},
        cwd="/Users/jack/project/remote-coding",
        session_id="sess-123",
        session_title="Example session",
    )

    assert prompt_input.tool_name == "Edit"
    assert prompt_input.tool_input == {"file_path": "/x.py"}
    with pytest.raises(FrozenInstanceError):
        prompt_input.session_title = "Other"  # type: ignore[misc]


def test_registry_resolves_full_tool_use_id_from_short_token() -> None:
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "abc12345", clock=lambda: 100.0)
    tool_use_id = "toolu_" + "x" * 200

    token = registry.register(tool_use_id)

    assert token == "abc12345"
    assert registry.resolve(token) == tool_use_id
    assert len(token.encode("utf-8")) < len(tool_use_id.encode("utf-8"))


def test_registry_expires_tokens() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = PermissionCallbackRegistry(ttl_sec=10, token_factory=lambda: "token001", clock=clock)
    token = registry.register("tool-1")

    now = 111.0

    assert registry.resolve(token) is None


def test_registry_retries_live_token_collision() -> None:
    tokens = iter(["same001", "same001", "next002"])
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: next(tokens), clock=lambda: 100.0)

    first = registry.register("tool-1")
    second = registry.register("tool-2")

    assert first == "same001"
    assert second == "next002"
    assert registry.resolve(first) == "tool-1"
    assert registry.resolve(second) == "tool-2"
