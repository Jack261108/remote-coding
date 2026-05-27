from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.permission_callback_registry import (
    AuthorizationMode,
    CallbackRecordStatus,
    ConsumeAlreadyResponded,
    ConsumeConsumed,
    ConsumeDispatchFailed,
    ConsumeNotFound,
    ConsumeUnauthorized,
    PermissionAction,
    PermissionCallbackRecord,
    PermissionCallbackRegistry,
    PreflightAlreadyResponded,
    PreflightDispatchFailed,
    PreflightEligible,
    PreflightNotFound,
    PreflightNotUnbound,
    PreflightUnauthorized,
    SessionOrigin,
)

BASE_SECONDS = 1_000.0
USER_ID = 42
OTHER_USER_ID = 99
TOKEN = "tok00001"


def _dt(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _auth_mode_and_users(auth_case: str) -> tuple[AuthorizationMode, frozenset[int]]:
    if auth_case == "all_users":
        return AuthorizationMode.ALL_USERS, frozenset()
    if auth_case == "listed":
        return AuthorizationMode.ALLOWED_USERS_SNAPSHOT, frozenset({USER_ID, OTHER_USER_ID})
    return AuthorizationMode.ALLOWED_USERS_SNAPSHOT, frozenset({OTHER_USER_ID})


def _is_authorized(auth_case: str) -> bool:
    return auth_case in {"all_users", "listed"}


def _record(status: CallbackRecordStatus, origin: SessionOrigin, auth_case: str, *, expired: bool) -> PermissionCallbackRecord:
    auth_mode, authorized_user_ids = _auth_mode_and_users(auth_case)
    responded = status in {
        CallbackRecordStatus.CLAIMED,
        CallbackRecordStatus.RESOLVED,
        CallbackRecordStatus.DISPATCH_FAILED,
        CallbackRecordStatus.SESSION_ENDED,
    }
    expires_at = _dt(BASE_SECONDS - 1 if expired else BASE_SECONDS + 60)
    return PermissionCallbackRecord(
        token=TOKEN,
        tool_use_id="tool-1",
        session_id="session-1",
        origin=origin,
        authorization_mode=auth_mode,
        authorized_user_ids=authorized_user_ids,
        created_at=_dt(BASE_SECONDS),
        expires_at=expires_at,
        status=status,
        decision=PermissionAction.ALLOW if responded else None,
        responded_by_user_id=OTHER_USER_ID if responded else None,
        responded_at=_dt(BASE_SECONDS + 1) if responded else None,
        dispatch_error_reason="backend_down" if status is CallbackRecordStatus.DISPATCH_FAILED else None,
    )


def _registry_with(record: PermissionCallbackRecord) -> PermissionCallbackRegistry:
    registry = PermissionCallbackRegistry(
        ttl_sec=60,
        token_factory=lambda: "unused01",
        clock=lambda: BASE_SECONDS,
        wall_clock=lambda: _dt(BASE_SECONDS),
    )
    registry._records[record.token] = record
    registry._compound_index[(record.session_id, record.tool_use_id)] = record.token
    return registry


def _expected_consume_type(status: CallbackRecordStatus, auth_case: str, *, expired: bool) -> type[object]:
    if status in {CallbackRecordStatus.SESSION_ENDED, CallbackRecordStatus.SUPERSEDED}:
        return ConsumeNotFound
    if status is CallbackRecordStatus.PENDING and expired:
        return ConsumeNotFound
    if not _is_authorized(auth_case):
        return ConsumeUnauthorized
    if status is CallbackRecordStatus.DISPATCH_FAILED:
        return ConsumeDispatchFailed
    if status in {CallbackRecordStatus.CLAIMED, CallbackRecordStatus.RESOLVED}:
        return ConsumeAlreadyResponded
    return ConsumeConsumed


def _expected_preflight_type(status: CallbackRecordStatus, origin: SessionOrigin, auth_case: str, *, expired: bool) -> type[object]:
    if status in {CallbackRecordStatus.SESSION_ENDED, CallbackRecordStatus.SUPERSEDED}:
        return PreflightNotFound
    if status is CallbackRecordStatus.PENDING and expired:
        return PreflightNotFound
    if not _is_authorized(auth_case):
        return PreflightUnauthorized
    if status is CallbackRecordStatus.DISPATCH_FAILED:
        return PreflightDispatchFailed
    if status in {CallbackRecordStatus.CLAIMED, CallbackRecordStatus.RESOLVED}:
        return PreflightAlreadyResponded
    if origin is SessionOrigin.EXTERNAL_UNBOUND:
        return PreflightEligible
    return PreflightNotUnbound


@settings(max_examples=100, deadline=None)
@given(
    status=st.sampled_from(list(CallbackRecordStatus)),
    origin=st.sampled_from(list(SessionOrigin)),
    auth_case=st.sampled_from(["all_users", "listed", "unlisted"]),
    expired=st.booleans(),
    action=st.sampled_from(list(PermissionAction)),
)
def test_consume_and_preflight_follow_auth_first_decision_table(
    status: CallbackRecordStatus,
    origin: SessionOrigin,
    auth_case: str,
    expired: bool,
    action: PermissionAction,
) -> None:
    preflight_registry = _registry_with(_record(status, origin, auth_case, expired=expired))
    consume_registry = _registry_with(_record(status, origin, auth_case, expired=expired))

    preflight_result = asyncio.run(preflight_registry.inspect_for_auto_approve_preflight(TOKEN, USER_ID))
    consume_result = asyncio.run(consume_registry.consume(TOKEN, USER_ID, action))

    expected_preflight_type = _expected_preflight_type(status, origin, auth_case, expired=expired)
    expected_consume_type = _expected_consume_type(status, auth_case, expired=expired)

    assert isinstance(preflight_result, expected_preflight_type)
    assert isinstance(consume_result, expected_consume_type)

    if not _is_authorized(auth_case) and status in {
        CallbackRecordStatus.DISPATCH_FAILED,
        CallbackRecordStatus.CLAIMED,
        CallbackRecordStatus.RESOLVED,
    }:
        assert isinstance(preflight_result, PreflightUnauthorized)
        assert isinstance(consume_result, ConsumeUnauthorized)

    if isinstance(preflight_result, (PreflightEligible, PreflightNotUnbound)):
        assert preflight_result.snapshot.status is CallbackRecordStatus.PENDING
        assert preflight_result.snapshot.token == TOKEN

    if isinstance(consume_result, ConsumeConsumed):
        assert consume_result.snapshot.status is CallbackRecordStatus.CLAIMED
        assert consume_result.snapshot.decision is action
        assert consume_result.snapshot.responded_by_user_id == USER_ID
        assert consume_result.snapshot.responded_at == _dt(BASE_SECONDS)
        assert consume_registry._records[TOKEN].status is CallbackRecordStatus.CLAIMED

    if isinstance(consume_result, ConsumeDispatchFailed):
        assert consume_result.reason == "backend_down"
    if isinstance(preflight_result, PreflightDispatchFailed):
        assert preflight_result.reason == "backend_down"


def test_dispatch_failed_without_reason_raises_internal_assertion() -> None:
    preflight_record = _record(CallbackRecordStatus.DISPATCH_FAILED, SessionOrigin.EXTERNAL_UNBOUND, "all_users", expired=False)
    consume_record = _record(CallbackRecordStatus.DISPATCH_FAILED, SessionOrigin.EXTERNAL_UNBOUND, "all_users", expired=False)
    preflight_record.dispatch_error_reason = None
    consume_record.dispatch_error_reason = None

    with pytest.raises(AssertionError, match="missing dispatch_error_reason"):
        asyncio.run(_registry_with(preflight_record).inspect_for_auto_approve_preflight(TOKEN, USER_ID))
    with pytest.raises(AssertionError, match="missing dispatch_error_reason"):
        asyncio.run(_registry_with(consume_record).consume(TOKEN, USER_ID, PermissionAction.ALLOW))


@settings(max_examples=20, deadline=None)
@given(user_id=st.integers(min_value=1, max_value=10_000), action=st.sampled_from(list(PermissionAction)))
def test_missing_tokens_are_not_found(user_id: int, action: PermissionAction) -> None:
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "unused01", clock=lambda: BASE_SECONDS)

    preflight_result = asyncio.run(registry.inspect_for_auto_approve_preflight("missing1", user_id))
    consume_result = asyncio.run(registry.consume("missing1", user_id, action))

    assert isinstance(preflight_result, PreflightNotFound)
    assert isinstance(consume_result, ConsumeNotFound)
