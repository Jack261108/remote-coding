from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers.callback_utils import apply_callback_response
from app.bot.handlers.user_utils import extract_user_id

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer
    from app.services.external_user_question_state import ExternalUserQuestionState
    from app.services.permission_gateway import PermissionGateway
    from app.services.unbound_permission_handler import UnboundPermissionHandler

logger = logging.getLogger(__name__)


def register_external_permission_handler(
    router: Router,
    *,
    hook_socket_server: HookSocketServer,
    unbound_permission_handler: UnboundPermissionHandler,
    external_uq_state: ExternalUserQuestionState | None = None,
    permission_gateway: PermissionGateway,
) -> None:
    @router.callback_query(F.data.startswith("ext_perm:"))
    async def handle_external_permission_callback(callback: CallbackQuery, callback_parts: tuple[str, ...]) -> None:
        _, token, decision = callback_parts
        if decision not in ("allow", "deny", "auto_approve"):
            await callback.answer("Invalid decision", show_alert=True)
            return

        user_id = extract_user_id(callback)
        response = await permission_gateway.handle_callback(data=f"perm:{token}:{decision}", user_id=user_id)
        await apply_callback_response(
            callback,
            edit_text=response.edit_message_text,
            clear_keyboard=response.clear_keyboard,
            alert_text=response.alert_text,
            show_alert=response.show_alert,
            log_prefix="external permission",
        )

    @router.callback_query(F.data.startswith("ext_uq:"))
    async def handle_external_user_question_callback(callback: CallbackQuery, callback_parts: tuple[str, ...]) -> None:
        """Handle user clicking an AskUserQuestion option button for external sessions."""
        from app.adapters.process.pty_injector import inject_option_selection

        _, tool_use_id, option_index_str = callback_parts
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

        user_id = extract_user_id(callback)
        selected_label = prompt.options[option_index].label

        # Determine if this is the final question (submit after selection)
        is_final = len(pending.prompts) == 1

        # Inject the selection into the terminal
        ok, err = await inject_option_selection(
            pending.pane_id,
            option_index=option_index,
            submit_after=is_final,
            tmux_bin=pending.tmux_bin,
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

        # Edit original message to reflect selection when the Telegram message object supports it.
        if callback.message and hasattr(callback.message, "edit_text"):
            original_text = callback.message.text or ""  # type: ignore[union-attr]
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
