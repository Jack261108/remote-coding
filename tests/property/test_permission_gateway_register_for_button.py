from __future__ import annotations

from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.auto_approve_service import AutoApproveService
from app.services.permission_callback_registry import AuthorizationMode, InFlightConflictError, SessionOrigin
from app.services.permission_gateway import PermissionGateway, RegisterForButtonConflict, RegisterForButtonOk

TOKEN = "AbCd12_-"
USER_ID = 42
OTHER_USER_ID = 99
SESSION_ID = "session-1"
TOOL_USE_ID = "tool-1"


class RecordingRegistry:
    def __init__(self, *, token: str = TOKEN, conflict: bool = False) -> None:
        self.token = token
        self.conflict = conflict
        self.calls: list[dict[str, object]] = []

    async def register_token(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        if self.conflict:
            raise InFlightConflictError("claimed")
        return self.token


class ActiveOwnerService(AutoApproveService):
    def __init__(self, active_owner_user_id: int | None) -> None:
        super().__init__()
        self._active_owner_user_id = active_owner_user_id

    def get_active_user_for_session(self, session_id: str) -> int | None:
        assert session_id == SESSION_ID
        return self._active_owner_user_id


def _gateway(*, registry: RecordingRegistry, aas: AutoApproveService, allow_all_users: bool, allowed_ids: set[int]) -> PermissionGateway:
    settings = SimpleNamespace(allow_all_users=allow_all_users, allowed_user_id_set=allowed_ids)
    return PermissionGateway(
        registry=registry,
        auto_approve_service=aas,
        task_service=SimpleNamespace(),
        hook_socket_server=SimpleNamespace(),
        unbound_responder=SimpleNamespace(),
        settings=settings,
        message_sender=SimpleNamespace(),
        message_builder=SimpleNamespace(),
    )


@settings(max_examples=80, deadline=None)
@given(
    origin=st.sampled_from(list(SessionOrigin)),
    has_active_owner=st.booleans(),
    allow_all_users=st.booleans(),
    include_user=st.booleans(),
)
@pytest.mark.asyncio
async def test_register_for_button_records_deterministic_authorization_metadata(
    origin: SessionOrigin,
    has_active_owner: bool,
    allow_all_users: bool,
    include_user: bool,
) -> None:
    allowed_ids = {USER_ID} if include_user else {OTHER_USER_ID}
    active_owner = USER_ID if has_active_owner else None
    registry = RecordingRegistry()
    gateway = _gateway(
        registry=registry,
        aas=ActiveOwnerService(active_owner),
        allow_all_users=allow_all_users,
        allowed_ids=allowed_ids,
    )

    result = await gateway.register_for_button(
        tool_use_id=TOOL_USE_ID,
        session_id=SESSION_ID,
        origin=origin,
        candidate_user_id=USER_ID,
    )

    assert isinstance(result, RegisterForButtonOk)
    assert len(registry.calls) == 1
    call = registry.calls[0]
    assert call["tool_use_id"] == TOOL_USE_ID
    assert call["session_id"] == SESSION_ID
    assert call["origin"] is origin

    if origin is SessionOrigin.OWNED:
        assert call["authorization_mode"] is AuthorizationMode.OWNER
        assert call["authorized_user_ids"] == frozenset({USER_ID})
    elif origin is SessionOrigin.EXTERNAL_BOUND:
        assert call["authorization_mode"] is AuthorizationMode.BOUND_USER
        assert call["authorized_user_ids"] == frozenset({USER_ID})
    elif has_active_owner:
        assert call["authorization_mode"] is AuthorizationMode.SOLE_AUTO_APPROVE_USER
        assert call["authorized_user_ids"] == frozenset({USER_ID})
    elif allow_all_users:
        assert call["authorization_mode"] is AuthorizationMode.ALL_USERS
        assert call["authorized_user_ids"] == frozenset()
    else:
        assert call["authorization_mode"] is AuthorizationMode.ALLOWED_USERS_SNAPSHOT
        assert call["authorized_user_ids"] == frozenset(allowed_ids)


@pytest.mark.asyncio
async def test_register_for_button_reuses_one_token_for_all_actions() -> None:
    registry = RecordingRegistry(token=TOKEN)
    gateway = _gateway(registry=registry, aas=AutoApproveService(), allow_all_users=False, allowed_ids={USER_ID})

    result = await gateway.register_for_button(
        tool_use_id=TOOL_USE_ID,
        session_id=SESSION_ID,
        origin=SessionOrigin.OWNED,
        candidate_user_id=USER_ID,
    )

    assert isinstance(result, RegisterForButtonOk)
    callback_data = [button.callback_data for row in result.keyboard.rows for button in row]
    assert callback_data == [f"perm:{TOKEN}:allow", f"perm:{TOKEN}:deny", f"perm:{TOKEN}:auto_approve"]
    assert len(registry.calls) == 1


@pytest.mark.asyncio
async def test_register_for_button_conflict_returns_advisory_keyboard() -> None:
    registry = RecordingRegistry(conflict=True)
    gateway = _gateway(registry=registry, aas=AutoApproveService(), allow_all_users=False, allowed_ids={USER_ID})

    result = await gateway.register_for_button(
        tool_use_id=TOOL_USE_ID,
        session_id=SESSION_ID,
        origin=SessionOrigin.OWNED,
        candidate_user_id=USER_ID,
    )

    assert isinstance(result, RegisterForButtonConflict)
    assert result.advisory_text == "权限请求处理中，请稍候"
    assert [button.callback_data for row in result.keyboard.rows for button in row] == ["perm:conflict:wait"]
    assert len(registry.calls) == 1
