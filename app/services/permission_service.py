from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.adapters.claude.hook_socket_server import HookSocketServer
from app.domain.session_models import SessionEvent, SessionEventType, SessionState
from app.services.lock_registry import RefCountedLockRegistry
from app.services.session_service import SessionService
from app.services.session_store import SessionStore

_VALID_PERMISSION_DECISIONS = {"allow", "deny"}


class PermissionService:
    def __init__(
        self,
        *,
        session_service: SessionService,
        structured_session_store: SessionStore | None,
        hook_socket_server: HookSocketServer | None,
        get_structured_session: Callable[..., Awaitable[SessionState | None]],
        is_state_owned_by_user: Callable[..., Awaitable[bool]],
        permission_lock_ttl_sec: int = 600,
        lock_cleanup_interval_sec: int = 60,
        lock_cleanup_batch_size: int = 50,
    ) -> None:
        self._session_service = session_service
        self._structured_session_store = structured_session_store
        self._hook_socket_server = hook_socket_server
        self._get_structured_session = get_structured_session
        self._is_state_owned_by_user = is_state_owned_by_user
        self._permission_locks = RefCountedLockRegistry(
            ttl_sec=permission_lock_ttl_sec,
            cleanup_interval_sec=lock_cleanup_interval_sec,
            cleanup_batch_size=lock_cleanup_batch_size,
        )

    async def respond_to_pending_permission(
        self,
        *,
        user_id: int,
        decision: str,
        reason: str | None = None,
        expected_tool_use_id: str | None = None,
    ) -> tuple[bool, str]:
        if decision not in _VALID_PERMISSION_DECISIONS:
            return False, "无效的权限决策"
        if self._structured_session_store is None or self._hook_socket_server is None:
            return False, "当前未启用 Claude hooks 权限通道"
        session = await self._session_service.get(user_id)
        if session is None or session.provider != "claude_code":
            return False, "当前没有 Claude 会话"

        lock_tool_use_id = expected_tool_use_id
        if lock_tool_use_id is None:
            state, err = await self._resolve_pending_permission_state(user_id=user_id, expected_tool_use_id=None)
            if err is not None:
                return False, err
            pending = state.pending_permission if state is not None else None
            if pending is None:
                return False, "当前没有待处理的权限请求"
            lock_tool_use_id = pending.tool_use_id

        async with self._permission_locks.lock(lock_tool_use_id):
            return await self._respond_to_pending_permission_locked(
                user_id=user_id,
                decision=decision,
                reason=reason,
                expected_tool_use_id=expected_tool_use_id,
                lock_tool_use_id=lock_tool_use_id,
            )

    async def _resolve_pending_permission_state(
        self,
        *,
        user_id: int,
        expected_tool_use_id: str | None,
    ) -> tuple[SessionState | None, str | None]:
        state = None
        if expected_tool_use_id is not None:
            state = self._structured_session_store.find_by_pending_tool_use_id(expected_tool_use_id)
            if state is not None and not await self._is_state_owned_by_user(state=state, user_id=user_id):
                return None, "这个权限按钮已经过期，请等待最新的权限请求"
        if state is None:
            state = await self._get_structured_session(user_id, log_missing=False)
            if state is not None and not await self._is_state_owned_by_user(state=state, user_id=user_id):
                state = None
        pending = state.pending_permission if state is not None else None
        if pending is None:
            if expected_tool_use_id is not None:
                return None, "这个权限按钮已经过期，请等待最新的权限请求"
            return None, "当前没有待处理的权限请求"
        if expected_tool_use_id is not None and pending.tool_use_id != expected_tool_use_id:
            return None, "这个权限按钮已经过期，请等待最新的权限请求"
        return state, None

    async def _respond_to_pending_permission_locked(
        self,
        *,
        user_id: int,
        decision: str,
        reason: str | None,
        expected_tool_use_id: str | None,
        lock_tool_use_id: str,
    ) -> tuple[bool, str]:
        state, err = await self._resolve_pending_permission_state(user_id=user_id, expected_tool_use_id=expected_tool_use_id)
        if err is not None:
            return False, err
        pending = state.pending_permission if state is not None else None
        if pending is None:
            return False, "当前没有待处理的权限请求"
        if expected_tool_use_id is None and pending.tool_use_id != lock_tool_use_id:
            return False, "当前没有待处理的权限请求"

        tool_use_id = pending.tool_use_id
        sent = await self._hook_socket_server.respond_to_permission(tool_use_id=tool_use_id, decision=decision, reason=reason)
        if not sent:
            return False, "待处理权限请求已失效，请等待 Claude 重新发起"
        tool_name = pending.tool_name
        event_type = SessionEventType.PERMISSION_APPROVED if decision == "allow" else SessionEventType.PERMISSION_DENIED
        updated = self._structured_session_store.process(
            SessionEvent(
                session_id=state.session_id,
                type=event_type,
                payload={"tool_use_id": tool_use_id},
            )
        )
        tool_name = updated.last_tool_name or pending.tool_name
        action = "已批准" if decision == "allow" else "已拒绝"
        if tool_name:
            return True, f"{action}权限请求: {tool_name}"
        return True, f"{action}权限请求"
