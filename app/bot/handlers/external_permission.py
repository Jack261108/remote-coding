from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer
    from app.services.auto_approve_service import AutoApproveService
    from app.services.external_user_question_state import ExternalUserQuestionState
    from app.services.permission_callback_registry import PermissionCallbackRegistry
    from app.services.unbound_permission_handler import UnboundPermissionHandler

logger = logging.getLogger(__name__)

_STALE_EXTERNAL_PERMISSION_CALLBACK_TEXT = (
    "Permission button expired or bot restarted. Trigger the action again or wait for Claude to request permission again."
)


def register_external_permission_handler(
    router: Router,
    *,
    hook_socket_server: HookSocketServer,
    unbound_permission_handler: UnboundPermissionHandler,
    permission_callback_registry: PermissionCallbackRegistry,
    external_uq_state: ExternalUserQuestionState | None = None,
    auto_approve_service: AutoApproveService | None = None,
) -> None:
    @router.callback_query(F.data.startswith("ext_perm:"))
    async def handle_external_permission_callback(callback: CallbackQuery) -> None:
        data = callback.data or ""
        parts = data.split(":")
        if len(parts) != 3:
            await callback.answer("Invalid callback data", show_alert=True)
            return

        _, token, decision = parts
        if decision not in ("allow", "deny", "auto_approve"):
            await callback.answer("Invalid decision", show_alert=True)
            return

        # Resolve short token to full tool_use_id
        tool_use_id = permission_callback_registry.resolve(token)
        if tool_use_id is None:
            await callback.answer(_STALE_EXTERNAL_PERMISSION_CALLBACK_TEXT, show_alert=True)
            return

        user_id = callback.from_user.id if callback.from_user else 0

        # For auto_approve: approve the current request + activate auto-approve for the session
        if decision == "auto_approve":
            # Resolve session_id from pending state
            session_id: str | None = None
            if unbound_permission_handler.is_unbound_permission(tool_use_id):
                session_id = unbound_permission_handler.get_session_id(tool_use_id)
                result = await unbound_permission_handler.handle_response(
                    tool_use_id=tool_use_id,
                    user_id=user_id,
                    decision="allow",
                )
                if not result.accepted:
                    await callback.answer("Already responded by another user", show_alert=True)
                    return
            else:
                session_id = await hook_socket_server.get_session_id_for_tool_use_id(tool_use_id)
                success = await hook_socket_server.respond_to_permission(
                    tool_use_id=tool_use_id,
                    decision="allow",
                    reason=f"auto-approve activated by user {user_id}",
                )
                if not success:
                    await callback.answer("Permission request expired or not found", show_alert=True)
                    return

            # Activate auto-approve for the session
            activation_failed = False
            if session_id and auto_approve_service is not None:
                activation_failed = not await auto_approve_service.activate_if_session_alive(user_id=user_id, session_id=session_id)

            if activation_failed:
                confirmation = "Permission approved, but session ended; auto-approve was not activated."
                edit_suffix = confirmation
                log_message = "external permission approved but auto-approve not activated"
            else:
                confirmation = "🟢 Auto-approve activated"
                edit_suffix = "🟢 已开启自动批准，本次会话后续权限请求将自动通过\n发送 /deny 可关闭"
                log_message = "external permission auto-approve activated"

            await callback.answer(confirmation)

            # Edit original message to reflect decision
            if callback.message:
                original_text = callback.message.text or ""
                await callback.message.edit_text(f"{original_text}\n\n{edit_suffix}")

            logger.info(
                log_message,
                extra={
                    "tool_use_id": tool_use_id,
                    "session_id": session_id,
                    "user_id": user_id,
                },
            )
            return

        # Try unbound first (first-responder-wins), then bound
        if unbound_permission_handler.is_unbound_permission(tool_use_id):
            result = await unbound_permission_handler.handle_response(
                tool_use_id=tool_use_id,
                user_id=user_id,
                decision=decision,
            )
            if not result.accepted:
                await callback.answer("Already responded by another user", show_alert=True)
                return
        else:
            # Bound session — respond directly via hook socket
            success = await hook_socket_server.respond_to_permission(
                tool_use_id=tool_use_id,
                decision=decision,
                reason=f"responded by user {user_id}",
            )
            if not success:
                await callback.answer("Permission request expired or not found", show_alert=True)
                return

        # Confirm to user
        emoji = "✅" if decision == "allow" else "❌"
        label = "Approved" if decision == "allow" else "Denied"
        await callback.answer(f"{emoji} {label}")

        # Edit original message to reflect decision
        if callback.message:
            original_text = callback.message.text or ""
            await callback.message.edit_text(f"{original_text}\n\n{emoji} {label} by you")

        logger.info(
            "external permission callback handled",
            extra={
                "tool_use_id": tool_use_id,
                "decision": decision,
                "user_id": user_id,
            },
        )

    @router.callback_query(F.data.startswith("ext_uq:"))
    async def handle_external_user_question_callback(callback: CallbackQuery) -> None:
        """Handle user clicking an AskUserQuestion option button for external sessions."""
        from app.adapters.process.pty_injector import inject_option_selection

        data = callback.data or ""
        parts = data.split(":")
        if len(parts) != 3:
            await callback.answer("Invalid callback data", show_alert=True)
            return

        _, tool_use_id, option_index_str = parts
        try:
            option_index = int(option_index_str)
        except ValueError:
            await callback.answer("Invalid option index", show_alert=True)
            return

        if external_uq_state is None:
            await callback.answer("Feature not available", show_alert=True)
            return

        pending = external_uq_state.get(tool_use_id)
        if pending is None:
            await callback.answer("Question expired or already answered", show_alert=True)
            return

        if pending.pane_id is None:
            await callback.answer("Cannot inject: no tmux pane found", show_alert=True)
            return

        # Validate option index
        prompt = pending.prompts[0] if pending.prompts else None
        if prompt is None or option_index < 0 or option_index >= len(prompt.options):
            await callback.answer("Invalid option", show_alert=True)
            return

        user_id = callback.from_user.id if callback.from_user else 0
        selected_label = prompt.options[option_index].label

        # Determine if this is the final question (submit after selection)
        is_final = len(pending.prompts) == 1

        # Inject the selection into the terminal
        ok, err = await inject_option_selection(
            pending.pane_id,
            option_index=option_index,
            submit_after=is_final,
        )
        if not ok:
            logger.warning(
                "pty injection failed for external user question",
                extra={"tool_use_id": tool_use_id, "pane_id": pending.pane_id, "error": err},
            )
            await callback.answer(f"Injection failed: {err}", show_alert=True)
            return

        # After successful injection on final question, allow the permission
        if is_final:
            await hook_socket_server.respond_to_permission(
                tool_use_id=tool_use_id,
                decision="allow",
                reason=f"AskUserQuestion answered via Telegram by user {user_id}",
            )

        # Clean up state
        external_uq_state.remove(tool_use_id)

        # Confirm to user
        await callback.answer(f"✅ Selected: {selected_label}")

        # Edit original message to reflect selection
        if callback.message:
            original_text = callback.message.text or ""
            await callback.message.edit_text(f"{original_text}\n\n✅ Selected: {selected_label} (by you)")

        logger.info(
            "external user question answered via Telegram",
            extra={
                "tool_use_id": tool_use_id,
                "option_index": option_index,
                "selected_label": selected_label,
                "user_id": user_id,
                "pane_id": pending.pane_id,
            },
        )
