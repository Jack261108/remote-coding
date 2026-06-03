from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.bot.presenters.permission_message_builder import PermissionMessageBuilder
from app.services.auto_approve_service import AutoApproveService, SlotClaimed
from app.services.permission_callback_registry import (
    AutoApproveOutcome,
    CallbackRecordStatus,
    PermissionAction,
    PermissionCallbackRegistry,
    SessionOrigin,
)
from app.services.permission_gateway import PermissionGateway, RegisterForButtonOk
from app.services.unbound_permission_handler import UnboundPermissionResponseResult


class _TokenFactory:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self) -> str:
        self.index += 1
        return f"tok{self.index:05d}"


class _TaskService:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, str, str | None]] = []

    async def respond_to_pending_permission(
        self,
        *,
        user_id: int | None,
        decision: str,
        reason: str | None = None,
        expected_tool_use_id: str | None = None,
    ) -> tuple[bool, str]:
        del reason
        self.calls.append((user_id, decision, expected_tool_use_id))
        return True, "ok"


class _HookSocketServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.respond_ok = True

    async def respond_to_permission(self, *, tool_use_id: str, decision: str, reason: str | None = None) -> bool:
        self.calls.append((tool_use_id, decision, reason))
        return self.respond_ok


class _UnboundResponder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []
        self.forwarded = True
        self.accepted = True

    async def handle_response(self, *, tool_use_id: str, user_id: int, decision: str) -> UnboundPermissionResponseResult:
        self.calls.append((tool_use_id, user_id, decision))
        return UnboundPermissionResponseResult(accepted=self.accepted, forwarded=self.forwarded)


class _MessageSender:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, *, keyboard: object | None = None, parse_mode: str | None = None) -> None:
        self.messages.append((chat_id, text))


class _Harness:
    def __init__(self, *, allow_all_users: bool = False, allowed_ids: set[int] | None = None) -> None:
        self.registry = PermissionCallbackRegistry(ttl_sec=600, token_factory=_TokenFactory())
        self.aas = AutoApproveService()
        self.task_service = _TaskService()
        self.hook_socket_server = _HookSocketServer()
        self.unbound_responder = _UnboundResponder()
        self.bot = _MessageSender()
        self.settings = SimpleNamespace(
            allow_all_users=allow_all_users,
            allowed_user_id_set={1, 2} if allowed_ids is None else allowed_ids,
        )
        self.gateway = PermissionGateway(
            registry=self.registry,
            auto_approve_service=self.aas,
            task_service=self.task_service,
            hook_socket_server=self.hook_socket_server,
            unbound_responder=self.unbound_responder,
            settings=self.settings,
            message_sender=self.bot,
            message_builder=PermissionMessageBuilder(),
        )

    async def register(self, *, origin: SessionOrigin, session_id: str, tool_use_id: str, user_id: int | None) -> str:
        result = await self.gateway.register_for_button(
            tool_use_id=tool_use_id,
            session_id=session_id,
            origin=origin,
            candidate_user_id=user_id,
        )
        assert isinstance(result, RegisterForButtonOk)
        callback_data = result.keyboard.rows[0][0].callback_data
        assert callback_data is not None
        _, token, _ = callback_data.split(":")
        return token

    def status(self, token: str) -> CallbackRecordStatus:
        return self.registry._records[token].status


@pytest.mark.asyncio
async def test_owned_register_clicks_and_subsequent_permission_auto_approved() -> None:
    harness = _Harness()

    allow_token = await harness.register(origin=SessionOrigin.OWNED, session_id="owned", tool_use_id="owned-allow", user_id=1)
    allow_response = await harness.gateway.handle_callback(data=f"perm:{allow_token}:allow", user_id=1)
    assert allow_response.alert_text == "已批准"
    assert harness.task_service.calls[-1] == (1, "allow", "owned-allow")
    assert harness.status(allow_token) is CallbackRecordStatus.RESOLVED

    deny_token = await harness.register(origin=SessionOrigin.OWNED, session_id="owned", tool_use_id="owned-deny", user_id=1)
    deny_response = await harness.gateway.handle_callback(data=f"perm:{deny_token}:deny", user_id=1)
    assert deny_response.alert_text == "已拒绝"
    assert harness.task_service.calls[-1] == (1, "deny", "owned-deny")
    assert harness.status(deny_token) is CallbackRecordStatus.RESOLVED

    auto_token = await harness.register(origin=SessionOrigin.OWNED, session_id="owned", tool_use_id="owned-auto", user_id=1)
    auto_response = await harness.gateway.handle_callback(data=f"perm:{auto_token}:auto_approve", user_id=1)
    assert auto_response.alert_text == "已开启自动批准"
    assert harness.task_service.calls[-1] == (1, "allow", "owned-auto")
    assert harness.aas.is_active(session_id="owned", user_id=1)

    outcome = await harness.gateway.maybe_auto_approve(
        session_id="owned",
        origin=SessionOrigin.OWNED,
        candidate_user_id=1,
        tool_use_id="owned-next",
        tool_name="Bash",
        tool_input={"command": "pwd"},
    )
    assert outcome is AutoApproveOutcome.APPROVED
    assert harness.task_service.calls[-1] == (1, "allow", "owned-next")


@pytest.mark.asyncio
async def test_external_bound_register_clicks_and_subsequent_permission_auto_approved() -> None:
    harness = _Harness()

    allow_token = await harness.register(origin=SessionOrigin.EXTERNAL_BOUND, session_id="bound", tool_use_id="bound-allow", user_id=1)
    allow_response = await harness.gateway.handle_callback(data=f"perm:{allow_token}:allow", user_id=1)
    assert allow_response.alert_text == "已批准"
    assert harness.hook_socket_server.calls[-1][0:2] == ("bound-allow", "allow")
    assert harness.status(allow_token) is CallbackRecordStatus.RESOLVED

    deny_token = await harness.register(origin=SessionOrigin.EXTERNAL_BOUND, session_id="bound", tool_use_id="bound-deny", user_id=1)
    deny_response = await harness.gateway.handle_callback(data=f"perm:{deny_token}:deny", user_id=1)
    assert deny_response.alert_text == "已拒绝"
    assert harness.hook_socket_server.calls[-1][0:2] == ("bound-deny", "deny")

    auto_token = await harness.register(origin=SessionOrigin.EXTERNAL_BOUND, session_id="bound", tool_use_id="bound-auto", user_id=1)
    auto_response = await harness.gateway.handle_callback(data=f"perm:{auto_token}:auto_approve", user_id=1)
    assert auto_response.alert_text == "已开启自动批准"
    assert harness.hook_socket_server.calls[-1][0:2] == ("bound-auto", "allow")
    assert harness.aas.is_active(session_id="bound", user_id=1)

    outcome = await harness.gateway.maybe_auto_approve(
        session_id="bound",
        origin=SessionOrigin.EXTERNAL_BOUND,
        candidate_user_id=1,
        tool_use_id="bound-next",
        tool_name="Edit",
        tool_input={"file_path": "x.py"},
    )
    assert outcome is AutoApproveOutcome.APPROVED
    assert harness.hook_socket_server.calls[-1][0:2] == ("bound-next", "allow")


@pytest.mark.asyncio
async def test_external_unbound_first_responder_and_auto_approve_slot_behaviour() -> None:
    harness = _Harness()

    token = await harness.register(origin=SessionOrigin.EXTERNAL_UNBOUND, session_id="unbound", tool_use_id="unbound-one", user_id=None)
    first, second = await asyncio.gather(
        harness.gateway.handle_callback(data=f"perm:{token}:allow", user_id=1),
        harness.gateway.handle_callback(data=f"perm:{token}:deny", user_id=2),
    )
    assert sorted([first.alert_text, second.alert_text]) == ["已响应过", "已批准"]
    assert len(harness.unbound_responder.calls) == 1
    assert harness.status(token) is CallbackRecordStatus.RESOLVED

    held = await harness.aas.try_claim_slot(session_id="unbound", user_id=1)
    assert isinstance(held, SlotClaimed)
    conflict_token = await harness.register(
        origin=SessionOrigin.EXTERNAL_UNBOUND, session_id="unbound", tool_use_id="unbound-conflict", user_id=None
    )
    conflict = await harness.gateway.handle_callback(data=f"perm:{conflict_token}:auto_approve", user_id=2)
    assert conflict.alert_text == "已被其他用户激活"
    assert harness.status(conflict_token) is CallbackRecordStatus.PENDING

    same_user = await harness.gateway.handle_callback(data=f"perm:{conflict_token}:auto_approve", user_id=1)
    assert same_user.alert_text == "正在处理自动批准"
    await harness.aas.release_slot(session_id="unbound", user_id=1, attempt_id=held.attempt_id)

    auto_token = await harness.register(
        origin=SessionOrigin.EXTERNAL_UNBOUND, session_id="unbound", tool_use_id="unbound-auto", user_id=None
    )
    auto_response = await harness.gateway.handle_callback(data=f"perm:{auto_token}:auto_approve", user_id=1)
    assert auto_response.alert_text == "已开启自动批准"
    assert harness.aas.is_active(session_id="unbound", user_id=1)

    deny_reply = await harness.gateway.handle_deny_command(user_id=1)
    assert deny_reply.startswith("已关闭自动批准")
    assert not harness.aas.is_active(session_id="unbound", user_id=1)
    assert harness.aas.get_active_user_for_session("unbound") is None


@pytest.mark.asyncio
async def test_unbound_active_owner_fallback_auto_approve_resolves_token_and_keeps_activation() -> None:
    harness = _Harness()
    await harness.aas.activate_if_session_alive(user_id=1, session_id="fallback")
    harness.unbound_responder.forwarded = False

    outcome = await harness.gateway.maybe_auto_approve(
        session_id="fallback",
        origin=SessionOrigin.EXTERNAL_UNBOUND,
        candidate_user_id=1,
        tool_use_id="fallback-auto-fail",
        tool_name="Bash",
        tool_input={"command": "false"},
    )
    assert outcome is AutoApproveOutcome.APPROVAL_FAILED

    harness.unbound_responder.forwarded = True
    token = await harness.register(origin=SessionOrigin.EXTERNAL_UNBOUND, session_id="fallback", tool_use_id="fallback-click", user_id=None)
    response = await harness.gateway.handle_callback(data=f"perm:{token}:auto_approve", user_id=1)

    assert response.alert_text == "已批准"
    assert harness.unbound_responder.calls[-1] == ("fallback-click", 1, "allow")
    assert harness.status(token) is CallbackRecordStatus.RESOLVED
    assert harness.aas.is_active(session_id="fallback", user_id=1)


@pytest.mark.asyncio
async def test_unbound_allow_all_users_mode_allows_any_telegram_user() -> None:
    harness = _Harness(allow_all_users=True, allowed_ids=set())
    token = await harness.register(
        origin=SessionOrigin.EXTERNAL_UNBOUND, session_id="all-users", tool_use_id="all-users-tool", user_id=None
    )

    response = await harness.gateway.handle_callback(data=f"perm:{token}:allow", user_id=999)

    assert response.alert_text == "已批准"
    assert harness.unbound_responder.calls == [("all-users-tool", 999, "allow")]
    assert harness.status(token) is CallbackRecordStatus.RESOLVED


@pytest.mark.asyncio
async def test_session_end_cleanup_marks_registry_and_clears_aas_state() -> None:
    harness = _Harness()
    pending = await harness.register(origin=SessionOrigin.OWNED, session_id="ending", tool_use_id="pending", user_id=1)
    claimed = await harness.register(origin=SessionOrigin.OWNED, session_id="ending", tool_use_id="claimed", user_id=1)
    failed = await harness.register(origin=SessionOrigin.OWNED, session_id="ending", tool_use_id="failed", user_id=1)
    resolved = await harness.register(origin=SessionOrigin.OWNED, session_id="ending", tool_use_id="resolved", user_id=1)

    await harness.registry.consume(claimed, 1, PermissionAction.ALLOW)
    await harness.registry.consume(failed, 1, PermissionAction.ALLOW)
    await harness.registry.mark_dispatch_failed(failed, "backend_rejected")
    await harness.registry.consume(resolved, 1, PermissionAction.ALLOW)
    await harness.registry.mark_resolved(resolved)
    slot = await harness.aas.try_claim_slot(session_id="ending", user_id=1)
    assert isinstance(slot, SlotClaimed)
    await harness.aas.activate_if_session_alive(user_id=1, session_id="ending")

    await harness.aas.deactivate_all_for_session("ending")
    await harness.aas.release_all_slots_for_session("ending")
    transitioned = await harness.registry.invalidate_session("ending")

    assert transitioned == 3
    assert harness.status(pending) is CallbackRecordStatus.SESSION_ENDED
    assert harness.status(claimed) is CallbackRecordStatus.SESSION_ENDED
    assert harness.status(failed) is CallbackRecordStatus.SESSION_ENDED
    assert harness.status(resolved) is CallbackRecordStatus.RESOLVED
    assert harness.aas.get_active_user_for_session("ending") is None
    assert "ending" not in harness.aas._slots


@pytest.mark.asyncio
async def test_process_restart_old_buttons_are_expired() -> None:
    first = _Harness()
    old_token = await first.register(origin=SessionOrigin.OWNED, session_id="restart", tool_use_id="restart-tool", user_id=1)
    restarted = _Harness()

    response = await restarted.gateway.handle_callback(data=f"perm:{old_token}:allow", user_id=1)

    assert response.alert_text == "按钮已过期，请重新触发请求"
    assert restarted.task_service.calls == []
