from __future__ import annotations

from collections.abc import Iterable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.auto_approve_service import AutoApproveService
from app.services.permission_callback_registry import (
    AuthorizationMode,
    CallbackRecordStatus,
    PermissionAction,
    PermissionCallbackRegistry,
    SessionOrigin,
)

_TARGET_SESSION = "target-session"
_OTHER_SESSION = "other-session"
_USER_ID = 42


class _TokenFactory:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self) -> str:
        self.index += 1
        return f"se{self.index:06d}"


async def _register_record(registry: PermissionCallbackRegistry, *, session_id: str, tool_use_id: str) -> str:
    return await registry.register_token(
        tool_use_id=tool_use_id,
        session_id=session_id,
        origin=SessionOrigin.EXTERNAL_UNBOUND,
        authorization_mode=AuthorizationMode.ALL_USERS,
        authorized_user_ids=frozenset(),
    )


async def _set_status(registry: PermissionCallbackRegistry, *, token: str, status: CallbackRecordStatus, user_id: int) -> None:
    if status is CallbackRecordStatus.PENDING:
        return
    if status in {
        CallbackRecordStatus.CLAIMED,
        CallbackRecordStatus.RESOLVED,
        CallbackRecordStatus.DISPATCH_FAILED,
    }:
        await registry.consume(token, user_id, PermissionAction.ALLOW)
        if status is CallbackRecordStatus.RESOLVED:
            await registry.mark_resolved(token)
        elif status is CallbackRecordStatus.DISPATCH_FAILED:
            await registry.mark_dispatch_failed(token, "backend_rejected")
        return
    registry._records[token].status = status


async def _seed_registry(registry: PermissionCallbackRegistry, statuses: Iterable[CallbackRecordStatus]) -> dict[str, CallbackRecordStatus]:
    expected_other_statuses: dict[str, CallbackRecordStatus] = {}
    for index, status in enumerate(statuses):
        session_id = _TARGET_SESSION if index % 2 == 0 else _OTHER_SESSION
        token = await _register_record(registry, session_id=session_id, tool_use_id=f"tool-{index}")
        await _set_status(registry, token=token, status=status, user_id=_USER_ID)
        if session_id == _OTHER_SESSION:
            expected_other_statuses[token] = registry._records[token].status
    return expected_other_statuses


async def _seed_aas(aas: AutoApproveService, *, target_active: bool, target_slot: bool, other_active: bool, other_slot: bool) -> None:
    if target_active:
        await aas.activate_if_session_alive(user_id=_USER_ID, session_id=_TARGET_SESSION)
    if target_slot:
        await aas.try_claim_slot(session_id=_TARGET_SESSION, user_id=_USER_ID)
    if other_active:
        await aas.activate_if_session_alive(user_id=_USER_ID + 1, session_id=_OTHER_SESSION)
    if other_slot:
        await aas.try_claim_slot(session_id=_OTHER_SESSION, user_id=_USER_ID + 1)


@settings(max_examples=80, deadline=None)
@given(
    statuses=st.lists(st.sampled_from(list(CallbackRecordStatus)), min_size=1, max_size=12),
    target_active=st.booleans(),
    target_slot=st.booleans(),
    other_active=st.booleans(),
    other_slot=st.booleans(),
)
@pytest.mark.asyncio
async def test_session_end_cleanup_removes_target_session_pending_claimed_failed_and_aas_state(
    statuses: list[CallbackRecordStatus],
    target_active: bool,
    target_slot: bool,
    other_active: bool,
    other_slot: bool,
) -> None:
    registry = PermissionCallbackRegistry(ttl_sec=600, token_factory=_TokenFactory())
    aas = AutoApproveService()
    expected_other_statuses = await _seed_registry(registry, statuses)
    await _seed_aas(
        aas,
        target_active=target_active,
        target_slot=target_slot,
        other_active=other_active,
        other_slot=other_slot,
    )

    await aas.deactivate_all_for_session(_TARGET_SESSION)
    await aas.release_all_slots_for_session(_TARGET_SESSION)
    await registry.invalidate_session(_TARGET_SESSION)

    for record in registry._records.values():
        if record.session_id == _TARGET_SESSION:
            assert record.status not in {
                CallbackRecordStatus.PENDING,
                CallbackRecordStatus.CLAIMED,
                CallbackRecordStatus.DISPATCH_FAILED,
            }
    assert aas.get_active_user_for_session(_TARGET_SESSION) is None
    assert _TARGET_SESSION not in aas._slots

    for token, expected_status in expected_other_statuses.items():
        assert registry._records[token].status is expected_status
    if other_active:
        assert aas.get_active_user_for_session(_OTHER_SESSION) == _USER_ID + 1
    if other_slot and not other_active:
        assert _OTHER_SESSION in aas._slots
