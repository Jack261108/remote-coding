from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.external_session_models import UnboundPermissionState
from app.domain.hook_models import HookEvent
from app.domain.models import utc_now

if TYPE_CHECKING:
    from aiogram import Bot

    from app.adapters.claude.hook_socket_server import HookSocketServer

logger = logging.getLogger(__name__)


class UnboundPermissionHandler:
    """Handles permission requests from unbound sessions.

    Broadcasts to all allowed users, first-responder-wins semantics,
    auto-deny on TTL expiry.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        hook_socket_server: HookSocketServer,
        allowed_user_ids: set[int],
        permission_ttl_sec: int = 600,
        title_resolver: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._bot = bot
        self._hook_socket_server = hook_socket_server
        self._allowed_user_ids = allowed_user_ids
        self._permission_ttl_sec = permission_ttl_sec
        self._title_resolver = title_resolver
        self._pending: dict[str, UnboundPermissionState] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}

    async def handle_unbound_permission(self, event: HookEvent) -> None:
        """Broadcast permission request to all allowed users.

        Steps:
        1. Send message to each user in allowed_user_ids with session_id, cwd, tool info
        2. Track the pending state
        3. Schedule TTL expiry task
        """
        tool_use_id = event.tool_use_id or ""
        if not tool_use_id:
            logger.warning(
                "unbound permission event missing tool_use_id, ignoring",
                extra={"session_id": event.session_id},
            )
            return

        notified_user_ids: list[int] = []
        message_text = self._format_permission_message(event)
        keyboard = self._build_permission_keyboard(tool_use_id)

        for user_id in self._allowed_user_ids:
            try:
                await self._bot.send_message(chat_id=user_id, text=message_text, reply_markup=keyboard)
                notified_user_ids.append(user_id)
            except Exception:
                logger.warning(
                    "failed to send unbound permission notification",
                    extra={"user_id": user_id, "tool_use_id": tool_use_id},
                )

        state = UnboundPermissionState(
            session_id=event.session_id,
            tool_use_id=tool_use_id,
            notified_user_ids=notified_user_ids,
            responded=False,
            responded_by=None,
            created_at=utc_now(),
        )
        self._pending[tool_use_id] = state

        # Schedule expiry
        self._cancel_expiry_task(tool_use_id)
        self._expiry_tasks[tool_use_id] = asyncio.create_task(self._expire_permission(tool_use_id))

        logger.info(
            "unbound permission broadcast sent",
            extra={
                "tool_use_id": tool_use_id,
                "session_id": event.session_id,
                "notified_count": len(notified_user_ids),
            },
        )

    async def handle_response(self, *, tool_use_id: str, user_id: int, decision: str) -> bool:
        """Process a response from a user. Returns True if this was the first response.

        Steps:
        1. Check if already responded → return False (discard)
        2. Mark as responded, record responding user
        3. Forward decision to hook socket
        4. Return True
        """
        state = self._pending.get(tool_use_id)
        if state is None:
            return False

        if state.responded:
            return False

        state.responded = True
        state.responded_by = user_id

        # Cancel expiry task since we have a response
        self._cancel_expiry_task(tool_use_id)

        # Forward decision to hook socket
        await self._hook_socket_server.respond_to_permission(
            tool_use_id=tool_use_id,
            decision=decision,
            reason=f"responded by user {user_id}",
        )

        logger.info(
            "unbound permission responded",
            extra={
                "tool_use_id": tool_use_id,
                "user_id": user_id,
                "decision": decision,
                "session_id": state.session_id,
            },
        )
        return True

    async def _expire_permission(self, tool_use_id: str) -> None:
        """Auto-deny on TTL expiry if no response received."""
        try:
            await asyncio.sleep(self._permission_ttl_sec)
        except asyncio.CancelledError:
            return

        state = self._pending.get(tool_use_id)
        if state is None or state.responded:
            return

        state.responded = True

        # Auto-deny via hook socket
        await self._hook_socket_server.respond_to_permission(
            tool_use_id=tool_use_id,
            decision="deny",
            reason="no user responded within TTL",
        )

        # Clean up
        self._expiry_tasks.pop(tool_use_id, None)

        logger.info(
            "unbound permission expired, auto-denied",
            extra={"tool_use_id": tool_use_id, "session_id": state.session_id},
        )

    def is_unbound_permission(self, tool_use_id: str) -> bool:
        """Check if a tool_use_id belongs to an unbound permission request."""
        return tool_use_id in self._pending

    def get_session_id(self, tool_use_id: str) -> str | None:
        """Get the session_id for an unbound permission request."""
        state = self._pending.get(tool_use_id)
        return state.session_id if state is not None else None

    def _build_permission_keyboard(self, tool_use_id: str) -> InlineKeyboardMarkup:
        """Build inline keyboard with approve, deny, and auto-approve buttons.

        Truncates tool_use_id if any callback_data would exceed 64 bytes.
        """
        # The longest prefix is "ext_perm:" + ":" + "auto_approve" = 22 chars
        # Max callback_data is 64 bytes. Calculate max tool_use_id length.
        prefix = "ext_perm:"
        longest_action = ":auto_approve"
        max_id_bytes = 64 - len((prefix + longest_action).encode("utf-8"))

        truncated_id = tool_use_id
        if len(f"{prefix}{tool_use_id}{longest_action}".encode("utf-8")) > 64:
            # Truncate tool_use_id to fit within 64 bytes
            encoded = tool_use_id.encode("utf-8")[:max_id_bytes]
            truncated_id = encoded.decode("utf-8", errors="ignore")

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"ext_perm:{truncated_id}:allow"),
                    InlineKeyboardButton(text="❌ Deny", callback_data=f"ext_perm:{truncated_id}:deny"),
                ],
                [
                    InlineKeyboardButton(text="🟢 Auto-approve All", callback_data=f"ext_perm:{truncated_id}:auto_approve"),
                ],
            ]
        )

    def _format_permission_message(self, event: HookEvent) -> str:
        """Format a human-readable permission request message."""
        tool_name = event.tool or "unknown tool"
        cwd = event.cwd
        session_id = event.session_id
        short_id = session_id[:8]

        # Resolve session title
        title: str | None = None
        if self._title_resolver is not None:
            try:
                title = self._title_resolver(session_id, cwd)
            except Exception:
                pass

        lines = ["🔐 Permission request from unbound session"]
        if title:
            lines.append(f"Session: {title} ({short_id})")
        else:
            lines.append(f"Session: {short_id}")
        lines.append(f"Directory: {cwd}")
        lines.append(f"Tool: {tool_name}")

        if event.tool_input:
            # Include a brief description from tool_input
            description = event.tool_input.get("description") or event.tool_input.get("command") or ""
            if description:
                # Truncate long descriptions
                if len(description) > 200:
                    description = description[:200] + "..."
                lines.append(f"Details: {description}")

        lines.append("")
        lines.append("Use the buttons below to respond.")

        return "\n".join(lines)

    def _cancel_expiry_task(self, tool_use_id: str) -> None:
        """Cancel an existing expiry task for the given tool_use_id."""
        task = self._expiry_tasks.pop(tool_use_id, None)
        if task is not None:
            task.cancel()
