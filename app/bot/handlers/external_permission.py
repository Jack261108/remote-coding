from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer
    from app.services.unbound_permission_handler import UnboundPermissionHandler

logger = logging.getLogger(__name__)


def register_external_permission_handler(
    router: Router,
    *,
    hook_socket_server: HookSocketServer,
    unbound_permission_handler: UnboundPermissionHandler,
) -> None:
    @router.callback_query(F.data.startswith("ext_perm:"))
    async def handle_external_permission_callback(callback: CallbackQuery) -> None:
        data = callback.data or ""
        parts = data.split(":")
        if len(parts) != 3:
            await callback.answer("Invalid callback data", show_alert=True)
            return

        _, tool_use_id, decision = parts
        if decision not in ("approve", "deny"):
            await callback.answer("Invalid decision", show_alert=True)
            return

        user_id = callback.from_user.id if callback.from_user else 0

        # Try unbound first (first-responder-wins), then bound
        if unbound_permission_handler.is_unbound_permission(tool_use_id):
            accepted = await unbound_permission_handler.handle_response(
                tool_use_id=tool_use_id,
                user_id=user_id,
                decision=decision,
            )
            if not accepted:
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
        emoji = "✅" if decision == "approve" else "❌"
        label = "Approved" if decision == "approve" else "Denied"
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
