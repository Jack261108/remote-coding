from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.permission_callback_registry import (
    AuthorizationMode,
    CallbackRecordStatus,
    PermissionAction,
    PermissionCallbackRecord,
    PermissionCallbackRegistry,
    SessionOrigin,
)

BASE_SECONDS = 2_000.0


def _dt(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _make_record(
    *,
    index: int,
    status: CallbackRecordStatus,
    origin: SessionOrigin,
    authorization_mode: AuthorizationMode,
    expired: bool,
) -> PermissionCallbackRecord:
    responded = status in {
        CallbackRecordStatus.CLAIMED,
        CallbackRecordStatus.RESOLVED,
        CallbackRecordStatus.DISPATCH_FAILED,
        CallbackRecordStatus.SESSION_ENDED,
    }
    return PermissionCallbackRecord(
        token=f"tok{index:05d}",
        tool_use_id=f"tool-{index}",
        session_id=f"session-{index % 3}",
        origin=origin,
        authorization_mode=authorization_mode,
        authorized_user_ids=frozenset({1, 2}) if authorization_mode is not AuthorizationMode.ALL_USERS else frozenset(),
        created_at=_dt(BASE_SECONDS + index),
        expires_at=_dt(BASE_SECONDS - 1 if expired else BASE_SECONDS + 60 + index),
        status=status,
        decision=PermissionAction.ALLOW if responded else None,
        responded_by_user_id=1 if responded else None,
        responded_at=_dt(BASE_SECONDS + 30 + index) if responded else None,
        dispatch_error_reason="dispatch_error" if status is CallbackRecordStatus.DISPATCH_FAILED else None,
    )


record_specs = st.lists(
    st.tuples(
        st.sampled_from(list(CallbackRecordStatus)),
        st.sampled_from(list(SessionOrigin)),
        st.sampled_from(list(AuthorizationMode)),
        st.booleans(),
    ),
    min_size=0,
    max_size=5,
)


@settings(max_examples=100, deadline=None)
@given(specs=record_specs, token_index=st.integers(min_value=0, max_value=8), user_id=st.integers(min_value=1, max_value=10))
def test_preflight_does_not_mutate_records_or_compound_index(
    specs: list[tuple[CallbackRecordStatus, SessionOrigin, AuthorizationMode, bool]],
    token_index: int,
    user_id: int,
) -> None:
    registry = PermissionCallbackRegistry(
        ttl_sec=60,
        token_factory=lambda: "unused01",
        clock=lambda: BASE_SECONDS,
        wall_clock=lambda: _dt(BASE_SECONDS),
    )
    for index, (status, origin, authorization_mode, expired) in enumerate(specs):
        record = _make_record(index=index, status=status, origin=origin, authorization_mode=authorization_mode, expired=expired)
        registry._records[record.token] = record
        registry._compound_index[(record.session_id, record.tool_use_id)] = record.token

    token = f"tok{token_index:05d}"
    before_records = repr(registry._records)
    before_compound_index = repr(registry._compound_index)

    asyncio.run(registry.inspect_for_auto_approve_preflight(token, user_id))

    assert repr(registry._records) == before_records
    assert repr(registry._compound_index) == before_compound_index
