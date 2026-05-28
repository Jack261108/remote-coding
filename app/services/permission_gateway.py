from __future__ import annotations

import asyncio
import inspect
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.auto_approve_service import (
    CommitSlotMismatch,
    CommitSlotSessionEnded,
    CommitSlotSucceeded,
    SlotActiveOwnerExists,
    SlotAlreadyClaimedBySameUser,
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
    ConsumeUnauthorized,
    InFlightConflictError,
    PermissionAction,
    PermissionCallbackRecordSnapshot,
    PreflightAlreadyResponded,
    PreflightDispatchFailed,
    PreflightEligible,
    PreflightNotFound,
    PreflightNotUnbound,
    PreflightUnauthorized,
    SessionOrigin,
)

if TYPE_CHECKING:
    from app.services.unbound_permission_handler import UnboundPermissionResponseResult

logger = logging.getLogger(__name__)

_NEW_CALLBACK_RE = re.compile(r"^perm:([A-Za-z0-9_-]{1,64}):(allow|deny|auto_approve)$")
_LEGACY_PERMISSION_RE = re.compile(r"^perm:(allow|deny|auto_approve):([^:]+)$")
_LEGACY_EXTERNAL_RE = re.compile(r"^ext_perm:([^:]+):(allow|deny|auto_approve)$")


@dataclass(frozen=True, slots=True)
class BackendDispatchSucceeded:
    pass


@dataclass(frozen=True, slots=True)
class BackendDispatchFailed:
    reason: str


@dataclass(frozen=True, slots=True)
class BackendDispatchUnknown:
    reason: str


BackendDispatchResult = BackendDispatchSucceeded | BackendDispatchFailed | BackendDispatchUnknown


@dataclass(frozen=True, slots=True)
class CallbackResponse:
    alert_text: str
    show_alert: bool
    edit_message_text: str
    clear_keyboard: bool


@dataclass(frozen=True, slots=True)
class RegisterForButtonOk:
    keyboard: InlineKeyboardMarkup


@dataclass(frozen=True, slots=True)
class RegisterForButtonConflict:
    advisory_text: str
    keyboard: InlineKeyboardMarkup


RegisterForButtonResult = RegisterForButtonOk | RegisterForButtonConflict


class UnboundResponderProtocol(Protocol):
    async def handle_response(
        self,
        *,
        tool_use_id: str,
        user_id: int,
        decision: str,
    ) -> UnboundPermissionResponseResult: ...


class PermissionGateway:
    def __init__(
        self,
        *,
        registry: object,
        auto_approve_service: object,
        task_service: object,
        hook_socket_server: object,
        unbound_responder: UnboundResponderProtocol,
        settings: object,
        bot: object,
        message_builder: object,
    ) -> None:
        self._registry = registry
        self._auto_approve_service = auto_approve_service
        self._task_service = task_service
        self._hook_socket_server = hook_socket_server
        self._unbound_responder = unbound_responder
        self._settings = settings
        self._bot = bot
        self.message_builder = message_builder

    async def register_for_button(
        self,
        *,
        tool_use_id: str,
        session_id: str,
        origin: SessionOrigin,
        candidate_user_id: int | None,
    ) -> RegisterForButtonResult:
        authorization_mode, authorized_user_ids = self._resolve_authorization(
            origin=origin,
            candidate_user_id=candidate_user_id,
            session_id=session_id,
        )
        try:
            token = await self._registry.register_token(
                tool_use_id=tool_use_id,
                session_id=session_id,
                origin=origin,
                authorization_mode=authorization_mode,
                authorized_user_ids=authorized_user_ids,
            )
        except InFlightConflictError:
            logger.error(
                "permission callback registration conflict",
                extra={"tool_use_id": tool_use_id, "session_id": session_id, "origin": origin.value},
            )
            return RegisterForButtonConflict(
                advisory_text="权限请求处理中，请稍候",
                keyboard=self._build_advisory_keyboard(),
            )
        except RuntimeError:
            logger.error(
                "permission callback token registration failed",
                extra={"tool_use_id": tool_use_id, "session_id": session_id, "origin": origin.value},
            )
            raise

        return RegisterForButtonOk(keyboard=self._build_permission_keyboard(token))

    async def handle_callback(self, *, data: str, user_id: int) -> CallbackResponse:
        parsed = self._parse_callback_data(data)
        if parsed is None:
            return self._response("无效的权限响应")

        token, action = parsed
        if action is PermissionAction.AUTO_APPROVE:
            preflight = await self._registry.inspect_for_auto_approve_preflight(token, user_id)
            if isinstance(preflight, PreflightEligible):
                return await self._handle_unbound_auto_approve(token, user_id)
            if isinstance(preflight, PreflightNotUnbound):
                return await self._handle_standard(token, action, user_id)
            return self._response(self._alert_for_preflight_result(preflight))

        return await self._handle_standard(token, action, user_id)

    async def handle_approve_command(self, user_id: int) -> str:
        pending = await self._registry.find_pending_for_user(user_id, sort_desc_by_created_at=True)
        if not pending:
            return "当前没有待处理的权限请求"

        snapshot = pending[0]
        consume_result = await self._registry.consume(snapshot.token, user_id, PermissionAction.ALLOW)
        if not isinstance(consume_result, ConsumeConsumed):
            return self._text_for_non_consumed(consume_result)

        dispatch_result = await self._dispatch_with_completion_tracking(consume_result.snapshot, PermissionAction.ALLOW)
        return await self._text_after_dispatch(
            token=snapshot.token,
            success_text="已批准",
            failed_text="审批结果发送失败，请重新触发请求",
            dispatch_result=dispatch_result,
        )

    async def handle_deny_command(self, *, user_id: int, reason: str | None = None) -> str:
        del reason
        deactivated_count = await self._auto_approve_service.deactivate_all_for_user(user_id)
        prefix = "已关闭自动批准" if deactivated_count > 0 else "自动批准未开启"

        pending = await self._registry.find_pending_for_user(user_id, sort_desc_by_created_at=True)
        if not pending:
            return f"{prefix}\n当前没有待处理的权限请求"

        snapshot = pending[0]
        consume_result = await self._registry.consume(snapshot.token, user_id, PermissionAction.DENY)
        if not isinstance(consume_result, ConsumeConsumed):
            return f"{prefix}\n{self._text_for_non_consumed(consume_result)}"

        dispatch_result = await self._dispatch_with_completion_tracking(consume_result.snapshot, PermissionAction.DENY)
        outcome = await self._text_after_dispatch(
            token=snapshot.token,
            success_text="已拒绝",
            failed_text="审批结果发送失败，请重新触发请求",
            dispatch_result=dispatch_result,
        )
        return f"{prefix}\n{outcome}"

    async def maybe_auto_approve(
        self,
        *,
        session_id: str,
        origin: SessionOrigin,
        candidate_user_id: int | None,
        tool_use_id: str,
        tool_name: str,
        tool_input: object,
    ) -> AutoApproveOutcome:
        del tool_input
        if candidate_user_id is None:
            return AutoApproveOutcome.NOT_APPROVED
        if not self._auto_approve_service.is_active(session_id=session_id, user_id=candidate_user_id):
            return AutoApproveOutcome.NOT_APPROVED

        async with self._auto_approve_service.per_user_lock(candidate_user_id):
            if origin is SessionOrigin.EXTERNAL_UNBOUND and not self._is_user_currently_allowed(candidate_user_id):
                await self._auto_approve_service.deactivate_and_release_for_user_session(
                    user_id=candidate_user_id,
                    session_id=session_id,
                )
                return AutoApproveOutcome.NOT_APPROVED
            if not self._auto_approve_service.is_active(session_id=session_id, user_id=candidate_user_id):
                return AutoApproveOutcome.NOT_APPROVED

            snapshot = self._synthesize_snapshot_for_dispatch(
                session_id=session_id,
                origin=origin,
                candidate_user_id=candidate_user_id,
                tool_use_id=tool_use_id,
            )
            dispatch_result = await self._dispatch_with_completion_tracking(snapshot, PermissionAction.AUTO_APPROVE)

        if isinstance(dispatch_result, BackendDispatchSucceeded):
            logger.info(
                "permission request auto-approved",
                extra={"session_id": session_id, "tool_name": tool_name, "tool_use_id": tool_use_id},
            )
            return AutoApproveOutcome.APPROVED
        if isinstance(dispatch_result, BackendDispatchFailed):
            logger.warning(
                "permission auto-approve dispatch failed",
                extra={
                    "session_id": session_id,
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "reason": dispatch_result.reason,
                },
            )
            return AutoApproveOutcome.APPROVAL_FAILED

        logger.warning(
            "permission auto-approve dispatch outcome unknown",
            extra={"session_id": session_id, "tool_name": tool_name, "tool_use_id": tool_use_id, "reason": dispatch_result.reason},
        )
        await self._bot.send_message(
            chat_id=candidate_user_id,
            text="本次请求的自动批准结果未知，请检查最近的操作是否已生效；如未生效请重新触发",
        )
        return AutoApproveOutcome.APPROVAL_UNKNOWN

    async def _handle_standard(self, token: str, action: PermissionAction, user_id: int) -> CallbackResponse:
        deny_epoch_before = self._auto_approve_service.deny_epoch(user_id) if action is PermissionAction.AUTO_APPROVE else None
        consume_result = await self._registry.consume(token, user_id, action)
        if not isinstance(consume_result, ConsumeConsumed):
            return self._response(self._alert_for_consume_result(consume_result))

        dispatch_result = await self._dispatch_with_completion_tracking(consume_result.snapshot, action)
        if isinstance(dispatch_result, BackendDispatchFailed):
            transitioned = await asyncio.shield(self._registry.mark_dispatch_failed(token, dispatch_result.reason))
            return self._response("上次发送审批结果失败，请重新触发请求" if transitioned else "会话已结束，按钮已失效")
        if isinstance(dispatch_result, BackendDispatchUnknown):
            transitioned = await asyncio.shield(self._registry.mark_dispatch_failed(token, "dispatch_unknown"))
            return self._response(
                "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发"
                if transitioned
                else "会话已结束；本次响应结果未知，后端可能已收到，请检查会话输出或重新触发"
            )

        transitioned = await asyncio.shield(self._registry.mark_resolved(token))
        if not transitioned:
            return self._response("会话已结束，按钮已失效")
        if action is PermissionAction.AUTO_APPROVE and consume_result.snapshot.origin is not SessionOrigin.EXTERNAL_UNBOUND:
            assert deny_epoch_before is not None
            return await self._activate_owned_or_bound(
                token=token,
                user_id=user_id,
                session_id=consume_result.snapshot.session_id,
                deny_epoch_before=deny_epoch_before,
            )
        if action is PermissionAction.DENY:
            return self._response("已拒绝")
        return self._response("已批准")

    async def _handle_unbound_auto_approve(self, token: str, user_id: int) -> CallbackResponse:
        preflight = await self._registry.inspect_for_auto_approve_preflight(token, user_id)
        if isinstance(preflight, PreflightNotUnbound):
            return await self._handle_standard(token, PermissionAction.AUTO_APPROVE, user_id)
        if not isinstance(preflight, PreflightEligible):
            return self._response(self._alert_for_preflight_result(preflight))

        deny_epoch_before = self._auto_approve_service.deny_epoch(user_id)
        slot_result = await self._auto_approve_service.try_claim_slot(session_id=preflight.snapshot.session_id, user_id=user_id)
        if isinstance(slot_result, SlotConflict):
            return self._response("已被其他用户激活")
        if isinstance(slot_result, SlotAlreadyClaimedBySameUser):
            return self._response("正在处理自动批准")
        if isinstance(slot_result, SlotActiveOwnerExists):
            return await self._handle_standard(token, PermissionAction.AUTO_APPROVE, user_id)
        if not isinstance(slot_result, SlotClaimed):
            return self._response("已被其他用户激活")

        slot_session_id = preflight.snapshot.session_id
        slot_attempt_id = slot_result.attempt_id
        slot_open = True
        try:
            consume_result = await self._registry.consume(token, user_id, PermissionAction.AUTO_APPROVE)
            if not isinstance(consume_result, ConsumeConsumed):
                released = await self._auto_approve_service.release_slot(
                    session_id=slot_session_id,
                    user_id=user_id,
                    attempt_id=slot_attempt_id,
                )
                if released:
                    slot_open = False
                return self._response(self._alert_for_consume_result(consume_result))

            dispatch_result = await self._dispatch_with_completion_tracking(consume_result.snapshot, PermissionAction.AUTO_APPROVE)
            if isinstance(dispatch_result, BackendDispatchFailed):
                transitioned = await asyncio.shield(self._registry.mark_dispatch_failed(token, dispatch_result.reason))
                released = await self._auto_approve_service.release_slot(
                    session_id=consume_result.snapshot.session_id,
                    user_id=user_id,
                    attempt_id=slot_attempt_id,
                )
                if released:
                    slot_open = False
                return self._response("上次发送审批结果失败，请重新触发请求" if transitioned else "会话已结束，按钮已失效")
            if isinstance(dispatch_result, BackendDispatchUnknown):
                transitioned = await asyncio.shield(self._registry.mark_dispatch_failed(token, "dispatch_unknown"))
                released = await self._auto_approve_service.release_slot(
                    session_id=consume_result.snapshot.session_id,
                    user_id=user_id,
                    attempt_id=slot_attempt_id,
                )
                if released:
                    slot_open = False
                return self._response(
                    "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发"
                    if transitioned
                    else "会话已结束；本次响应结果未知，后端可能已收到，请检查会话输出或重新触发"
                )

            async with self._auto_approve_service.per_user_lock(user_id):
                response, slot_open = await self._resolve_and_commit_unbound(
                    token=token,
                    snapshot=consume_result.snapshot,
                    user_id=user_id,
                    attempt_id=slot_attempt_id,
                    deny_epoch_before=deny_epoch_before,
                )
                return response
        finally:
            if slot_open:
                await asyncio.shield(
                    self._auto_approve_service.release_slot(
                        session_id=slot_session_id,
                        user_id=user_id,
                        attempt_id=slot_attempt_id,
                    )
                )

    async def _activate_owned_or_bound(
        self,
        *,
        token: str,
        user_id: int,
        session_id: str,
        deny_epoch_before: int,
    ) -> CallbackResponse:
        del token
        async with self._auto_approve_service.per_user_lock(user_id):
            if self._auto_approve_service.deny_epoch(user_id) != deny_epoch_before:
                return self._response("已批准本次请求；自动批准已被 /deny 取消")
            activated = await self._auto_approve_service.activate_if_session_alive(user_id=user_id, session_id=session_id)
            return self._response("已开启自动批准" if activated else "会话已结束，按钮已失效")

    async def _resolve_and_commit_unbound(
        self,
        *,
        token: str,
        snapshot: PermissionCallbackRecordSnapshot,
        user_id: int,
        attempt_id: str,
        deny_epoch_before: int,
    ) -> tuple[CallbackResponse, bool]:
        transitioned = await asyncio.shield(self._registry.mark_resolved(token))
        if not transitioned:
            released = await self._auto_approve_service.release_slot(
                session_id=snapshot.session_id,
                user_id=user_id,
                attempt_id=attempt_id,
            )
            return self._response("会话已结束，按钮已失效"), not released

        if self._auto_approve_service.deny_epoch(user_id) != deny_epoch_before:
            released = await self._auto_approve_service.release_slot(
                session_id=snapshot.session_id,
                user_id=user_id,
                attempt_id=attempt_id,
            )
            return self._response("已批准本次请求；自动批准已被 /deny 取消"), not released

        commit_result = await self._auto_approve_service.commit_slot_if_session_alive(
            session_id=snapshot.session_id,
            user_id=user_id,
            attempt_id=attempt_id,
        )
        if isinstance(commit_result, CommitSlotSucceeded):
            return self._response("已开启自动批准"), False
        if isinstance(commit_result, CommitSlotSessionEnded):
            released = await self._auto_approve_service.release_slot(
                session_id=snapshot.session_id,
                user_id=user_id,
                attempt_id=attempt_id,
            )
            return self._response("会话已结束，按钮已失效"), not released
        if isinstance(commit_result, CommitSlotMismatch):
            return self._response("已批准本次请求；自动批准未开启"), True
        return self._response("已批准本次请求；自动批准未开启"), True

    async def _dispatch_with_completion_tracking(
        self,
        snapshot: PermissionCallbackRecordSnapshot,
        action: PermissionAction,
    ) -> BackendDispatchResult:
        backend_decision = "deny" if action is PermissionAction.DENY else "allow"
        started = False
        try:
            if snapshot.origin is SessionOrigin.OWNED:
                responder = self._task_service.respond_to_pending_permission
                kwargs = self._owned_response_kwargs(snapshot=snapshot, backend_decision=backend_decision)
                started = True
                result = await responder(**kwargs)
                started = False
                return self._dispatch_result_from_backend_return(result)

            if snapshot.origin is SessionOrigin.EXTERNAL_BOUND:
                responder = self._hook_socket_server.respond_to_permission
                started = True
                result = await responder(
                    tool_use_id=snapshot.tool_use_id,
                    decision=backend_decision,
                    reason=f"responded by user {snapshot.responded_by_user_id}",
                )
                started = False
                return BackendDispatchSucceeded() if bool(result) else BackendDispatchFailed("backend_rejected")

            responder = self._unbound_responder.handle_response
            user_id = snapshot.responded_by_user_id
            if user_id is None:
                return BackendDispatchFailed("missing_responding_user")
            started = True
            result = await responder(tool_use_id=snapshot.tool_use_id, user_id=user_id, decision=backend_decision)
            started = False
            if not bool(getattr(result, "accepted", False)):
                return BackendDispatchFailed("unbound_not_accepted")
            if not bool(getattr(result, "forwarded", False)):
                return BackendDispatchFailed("unbound_not_forwarded")
            return BackendDispatchSucceeded()
        except asyncio.CancelledError:
            if started:
                return BackendDispatchUnknown("cancelled")
            return BackendDispatchFailed("cancelled_before_start")
        except Exception as exc:
            if started:
                return BackendDispatchUnknown(f"exception_after_start: {exc!r}")
            return BackendDispatchFailed(f"exception_before_start: {exc!r}")

    def _resolve_authorization(
        self,
        *,
        origin: SessionOrigin,
        candidate_user_id: int | None,
        session_id: str,
    ) -> tuple[AuthorizationMode, frozenset[int]]:
        if origin is SessionOrigin.OWNED:
            return AuthorizationMode.OWNER, frozenset({candidate_user_id}) if candidate_user_id is not None else frozenset()
        if origin is SessionOrigin.EXTERNAL_BOUND:
            return AuthorizationMode.BOUND_USER, frozenset({candidate_user_id}) if candidate_user_id is not None else frozenset()

        active_user_id = self._auto_approve_service.get_active_user_for_session(session_id)
        if active_user_id is not None:
            return AuthorizationMode.SOLE_AUTO_APPROVE_USER, frozenset({active_user_id})
        if self._settings.allow_all_users:
            return AuthorizationMode.ALL_USERS, frozenset()
        return AuthorizationMode.ALLOWED_USERS_SNAPSHOT, frozenset(self._settings.allowed_user_id_set)

    def _is_user_currently_allowed(self, user_id: int) -> bool:
        return bool(self._settings.allow_all_users or user_id in self._settings.allowed_user_id_set)

    def _synthesize_snapshot_for_dispatch(
        self,
        *,
        session_id: str,
        origin: SessionOrigin,
        candidate_user_id: int,
        tool_use_id: str,
    ) -> PermissionCallbackRecordSnapshot:
        now = datetime.now(timezone.utc)
        authorization_mode, authorized_user_ids = self._resolve_authorization(
            origin=origin,
            candidate_user_id=candidate_user_id,
            session_id=session_id,
        )
        return PermissionCallbackRecordSnapshot(
            token=f"auto:{tool_use_id}",
            tool_use_id=tool_use_id,
            session_id=session_id,
            origin=origin,
            authorization_mode=authorization_mode,
            authorized_user_ids=authorized_user_ids,
            created_at=now,
            expires_at=now,
            status=CallbackRecordStatus.CLAIMED,
            decision=PermissionAction.AUTO_APPROVE,
            responded_by_user_id=candidate_user_id,
            responded_at=now,
            dispatch_error_reason=None,
        )

    def _owned_response_kwargs(self, *, snapshot: PermissionCallbackRecordSnapshot, backend_decision: str) -> dict[str, object]:
        method = self._task_service.respond_to_pending_permission
        parameters = inspect.signature(method).parameters
        accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
        kwargs: dict[str, object] = {"user_id": snapshot.responded_by_user_id, "decision": backend_decision}
        if accepts_kwargs or "reason" in parameters:
            kwargs["reason"] = f"responded by user {snapshot.responded_by_user_id}"
        if accepts_kwargs or "expected_tool_use_id" in parameters:
            kwargs["expected_tool_use_id"] = snapshot.tool_use_id
        return kwargs

    def _dispatch_result_from_backend_return(self, result: object) -> BackendDispatchResult:
        if isinstance(result, tuple):
            success = bool(result[0]) if result else False
            reason = str(result[1]) if len(result) > 1 else "backend_rejected"
            return BackendDispatchSucceeded() if success else BackendDispatchFailed(reason)
        if isinstance(result, bool):
            return BackendDispatchSucceeded() if result else BackendDispatchFailed("backend_rejected")
        return BackendDispatchSucceeded() if bool(result) else BackendDispatchFailed("backend_rejected")

    async def _text_after_dispatch(
        self,
        *,
        token: str,
        success_text: str,
        failed_text: str,
        dispatch_result: BackendDispatchResult,
    ) -> str:
        if isinstance(dispatch_result, BackendDispatchSucceeded):
            transitioned = await asyncio.shield(self._registry.mark_resolved(token))
            return success_text if transitioned else "会话已结束，按钮已失效"
        if isinstance(dispatch_result, BackendDispatchFailed):
            transitioned = await asyncio.shield(self._registry.mark_dispatch_failed(token, dispatch_result.reason))
            return failed_text if transitioned else "会话已结束，按钮已失效"

        transitioned = await asyncio.shield(self._registry.mark_dispatch_failed(token, "dispatch_unknown"))
        return (
            "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发"
            if transitioned
            else "会话已结束；本次响应结果未知，后端可能已收到，请检查会话输出或重新触发"
        )

    def _text_for_non_consumed(self, result: object) -> str:
        if isinstance(result, (ConsumeUnauthorized, ConsumeNotFound)):
            return "当前没有待处理的权限请求"
        if isinstance(result, ConsumeAlreadyResponded):
            return "请求已被响应"
        if isinstance(result, ConsumeDispatchFailed):
            if result.reason == "dispatch_unknown":
                return "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发"
            return "审批结果发送失败，请重新触发请求"
        return "当前没有待处理的权限请求"

    def _alert_for_consume_result(self, result: object) -> str:
        if isinstance(result, ConsumeUnauthorized):
            return "无权限响应此请求"
        if isinstance(result, ConsumeAlreadyResponded):
            return "已响应过"
        if isinstance(result, ConsumeDispatchFailed):
            if result.reason == "dispatch_unknown":
                return "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发"
            return "上次发送审批结果失败，请重新触发请求"
        if isinstance(result, ConsumeNotFound):
            return "按钮已过期，请重新触发请求"
        return "无效的权限响应"

    def _alert_for_preflight_result(self, result: object) -> str:
        if isinstance(result, PreflightUnauthorized):
            return "无权限响应此请求"
        if isinstance(result, PreflightAlreadyResponded):
            return "已响应过"
        if isinstance(result, PreflightDispatchFailed):
            if result.reason == "dispatch_unknown":
                return "审批结果发送状态未知，请检查最近的操作是否已生效；如未生效请重新触发"
            return "上次发送审批结果失败，请重新触发请求"
        if isinstance(result, PreflightNotFound):
            return "按钮已过期，请重新触发请求"
        return "无效的权限响应"

    def _parse_callback_data(self, data: str) -> tuple[str, PermissionAction] | None:
        match = _NEW_CALLBACK_RE.fullmatch(data)
        if match is not None:
            token, action = match.groups()
            return token, PermissionAction(action)

        match = _LEGACY_PERMISSION_RE.fullmatch(data)
        if match is not None:
            action, token = match.groups()
            return token, PermissionAction(action)

        match = _LEGACY_EXTERNAL_RE.fullmatch(data)
        if match is not None:
            token, action = match.groups()
            return token, PermissionAction(action)

        return None

    def _build_permission_keyboard(self, token: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"perm:{token}:allow"),
                    InlineKeyboardButton(text="❌ Deny", callback_data=f"perm:{token}:deny"),
                ],
                [
                    InlineKeyboardButton(text="🟢 Auto-approve", callback_data=f"perm:{token}:auto_approve"),
                ],
            ]
        )

    def _build_advisory_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="权限请求处理中", callback_data="perm:conflict:wait")]])

    def _response(self, alert_text: str) -> CallbackResponse:
        return CallbackResponse(alert_text=alert_text, show_alert=True, edit_message_text="", clear_keyboard=True)
