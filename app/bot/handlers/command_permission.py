from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.task_service import TaskService

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer
    from app.services.auto_approve_service import AutoApproveService
    from app.services.session_store import SessionStore

logger = logging.getLogger(__name__)
_PERMISSION_CALLBACK_PREFIX = "perm"


_TELEGRAM_CALLBACK_DATA_MAX_BYTES = 64


def build_permission_callback_data(*, decision: str, tool_use_id: str) -> str:
    prefix = f"{_PERMISSION_CALLBACK_PREFIX}:{decision}:"
    prefix_len = len(prefix.encode("utf-8"))
    max_id_bytes = _TELEGRAM_CALLBACK_DATA_MAX_BYTES - prefix_len
    encoded_id = tool_use_id.encode("utf-8")
    if len(encoded_id) > max_id_bytes:
        # Truncate tool_use_id to fit within 64-byte limit
        tool_use_id = encoded_id[:max_id_bytes].decode("utf-8", errors="ignore")
    return f"{prefix}{tool_use_id}"


def parse_permission_callback_data(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None
    prefix, sep, rest = data.partition(":")
    if prefix != _PERMISSION_CALLBACK_PREFIX or not sep:
        return None
    decision, sep, tool_use_id = rest.partition(":")
    if not sep or decision not in {"allow", "deny", "auto_approve"} or not tool_use_id:
        return None
    return decision, tool_use_id


def build_permission_keyboard(*, tool_use_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="允许",
                    callback_data=build_permission_callback_data(decision="allow", tool_use_id=tool_use_id),
                ),
                InlineKeyboardButton(
                    text="拒绝",
                    callback_data=build_permission_callback_data(decision="deny", tool_use_id=tool_use_id),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="不再询问，全部允许",
                    callback_data=build_permission_callback_data(decision="auto_approve", tool_use_id=tool_use_id),
                ),
            ],
        ]
    )


def register_permission_handlers(
    router,
    *,
    task_service: TaskService,
    auto_approve_service: AutoApproveService | None = None,
    hook_socket_server: HookSocketServer | None = None,
    structured_session_store: SessionStore | None = None,
):
    @router.message(Command("approve"))
    async def command_approve(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        ok, text = await task_service.respond_to_pending_permission(user_id=user_id, decision="allow")
        if ok:
            await message.answer(text)
        else:
            await message.answer(f"批准失败: {text}")

    @router.message(Command("deny"))
    async def command_deny(message: Message, command: CommandObject) -> None:
        user_id = message.from_user.id if message.from_user else 0

        # Check if auto-approve is active for user's current session
        if auto_approve_service is not None:
            state = await task_service.get_structured_session(user_id, log_missing=False)
            if state is not None and auto_approve_service.get_active_session_for_user(user_id, state.session_id):
                auto_approve_service.deactivate(state.session_id)
                await message.answer("已关闭自动批准，后续权限请求将正常提示")
                return

        reason = (command.args or "").strip() or None
        ok, text = await task_service.respond_to_pending_permission(user_id=user_id, decision="deny", reason=reason)
        if ok:
            await message.answer(text)
        else:
            await message.answer(f"拒绝失败: {text}")

    @router.callback_query(F.data.startswith(f"{_PERMISSION_CALLBACK_PREFIX}:"))
    async def callback_permission(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        parsed = parse_permission_callback_data(callback.data)
        if parsed is None:
            await callback.answer("无效的权限操作", show_alert=True)
            return
        decision, tool_use_id = parsed

        if decision == "auto_approve":
            await _handle_auto_approve_callback(callback, user_id=user_id, tool_use_id=tool_use_id)
            return

        ok, text = await task_service.respond_to_pending_permission(
            user_id=user_id,
            decision=decision,
            expected_tool_use_id=tool_use_id,
        )
        if callback.message is not None:
            if ok:
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("failed to clear permission inline keyboard", extra={"user_id": user_id, "tool_use_id": tool_use_id})
                await callback.message.answer(text)
            else:
                await callback.message.answer(f"权限操作失败: {text}")
        await callback.answer(text, show_alert=not ok)

    async def _handle_auto_approve_callback(callback: CallbackQuery, *, user_id: int, tool_use_id: str) -> None:
        """Handle auto-approve button: approve current request + activate auto-approve for session."""
        # Resolve session_id before approving (approval clears pending state)
        session_id = await _resolve_session_id_for_tool_use_id(tool_use_id, user_id=user_id)

        # Approve the current permission (same as "allow")
        ok, text = await task_service.respond_to_pending_permission(
            user_id=user_id,
            decision="allow",
            expected_tool_use_id=tool_use_id,
        )

        if not ok:
            if callback.message is not None:
                await callback.message.answer(f"权限操作失败: {text}")
            await callback.answer(text, show_alert=True)
            return

        # Clear the inline keyboard
        if callback.message is not None:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("failed to clear permission inline keyboard", extra={"user_id": user_id, "tool_use_id": tool_use_id})

        # Activate auto-approve if we resolved the session_id
        if session_id and auto_approve_service is not None:
            auto_approve_service.activate(session_id, user_id=user_id)
            confirmation = "🟢 已开启自动批准，本次会话后续权限请求将自动通过\n发送 /deny 可关闭"
            if callback.message is not None:
                await callback.message.answer(confirmation)
            await callback.answer(confirmation)
        else:
            # Fallback: approved but couldn't activate auto-approve
            logger.warning(
                "auto-approve: could not resolve session_id, permission approved but auto-approve not activated",
                extra={"user_id": user_id, "tool_use_id": tool_use_id},
            )
            if callback.message is not None:
                await callback.message.answer(text)
            await callback.answer(text)

    async def _resolve_session_id_for_tool_use_id(tool_use_id: str, *, user_id: int) -> str | None:
        """Resolve the session_id associated with a pending permission's tool_use_id."""
        # Try structured session store first (matches by pending permission tool_use_id)
        if structured_session_store is not None:
            state = structured_session_store.find_by_pending_tool_use_id(tool_use_id)
            if state is not None:
                return state.session_id

        # Fallback: check hook_socket_server's pending permissions directly
        if hook_socket_server is not None:
            async with hook_socket_server._lock:
                pending = hook_socket_server._pending_permissions.get(tool_use_id)
                if pending is not None:
                    return pending.session_id
                # Try prefix match for truncated tool_use_ids
                for tid, perm in hook_socket_server._pending_permissions.items():
                    if tid.startswith(tool_use_id) or tool_use_id.startswith(tid):
                        return perm.session_id

        # Last resort: get session from the user's current structured session
        if structured_session_store is not None:
            state = await task_service.get_structured_session(user_id, log_missing=False)
            if state is not None and state.pending_permission is not None:
                return state.session_id

        return None
