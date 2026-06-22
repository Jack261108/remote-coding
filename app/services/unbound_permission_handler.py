from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain.external_session_models import UnboundPermissionState
from app.domain.hook_models import HookEvent
from app.domain.models import utc_now
from app.domain.permission_models import PermissionPromptInput
from app.infra.text_formatting import render_markdownish_to_telegram_html
from app.services.permission_callback_registry import SessionOrigin
from app.services.permission_gateway import RegisterForButtonConflict, RegisterForButtonOk

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer
    from app.services.message_sender import Keyboard, MessageSender
    from app.services.permission_gateway import PermissionGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UnboundPermissionResponseResult:
    accepted: bool
    forwarded: bool


class UnboundPermissionHandler:
    """Handles permission requests from unbound sessions.

    Broadcasts to all allowed users, first-responder-wins semantics,
    auto-deny on TTL expiry.
    """

    def __init__(
        self,
        *,
        message_sender: MessageSender,
        hook_socket_server: HookSocketServer,
        allowed_user_ids: set[int],
        permission_ttl_sec: int = 600,
        title_resolver: Callable[[str, str], str | None] | None = None,
        notify_user_ids_resolver: Callable[[], Awaitable[set[int]]] | None = None,
    ) -> None:
        self._message_sender = message_sender
        self._hook_socket_server = hook_socket_server
        self._allowed_user_ids = allowed_user_ids
        self._notify_user_ids_resolver = notify_user_ids_resolver
        self._permission_ttl_sec = permission_ttl_sec
        self._title_resolver = title_resolver
        self._permission_gateway: PermissionGateway | None = None
        self._pending: dict[str, UnboundPermissionState] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}
        self._state_lock = asyncio.Lock()

    def set_permission_gateway(self, gateway: PermissionGateway) -> None:
        self._permission_gateway = gateway

    async def handle_unbound_permission(self, event: HookEvent) -> None:
        """Broadcast permission request to all allowed users."""
        gateway = self._permission_gateway
        if gateway is None:
            raise RuntimeError("permission gateway is not configured")

        tool_use_id = event.tool_use_id or ""
        if not tool_use_id:
            logger.warning(
                "unbound permission event missing tool_use_id, ignoring",
                extra={"session_id": event.session_id},
            )
            return

        result = await gateway.register_for_button(
            tool_use_id=tool_use_id,
            session_id=event.session_id,
            origin=SessionOrigin.EXTERNAL_UNBOUND,
            candidate_user_id=None,
        )
        if isinstance(result, RegisterForButtonConflict):
            logger.warning(
                "unbound permission registration conflict",
                extra={"tool_use_id": tool_use_id, "session_id": event.session_id},
            )
            await self._broadcast(
                text=result.advisory_text,
                keyboard=result.keyboard,
                parse_mode=None,
            )
            return
        if not isinstance(result, RegisterForButtonOk):
            raise RuntimeError("unexpected permission gateway registration result")

        state = UnboundPermissionState(
            session_id=event.session_id,
            tool_use_id=tool_use_id,
            notified_user_ids=[],
            responded=False,
            responded_by=None,
            created_at=utc_now(),
        )
        async with self._state_lock:
            self._pending[tool_use_id] = state
            self._cancel_expiry_task(tool_use_id)
            self._expiry_tasks[tool_use_id] = asyncio.create_task(self._expire_permission(tool_use_id))

        prompt = PermissionPromptInput(
            tool_name=event.tool or "unknown tool",
            tool_input=event.tool_input,
            cwd=event.cwd,
            session_id=event.session_id,
            session_title=self._resolve_title(event.session_id, event.cwd),
        )
        text = render_markdownish_to_telegram_html(gateway.message_builder.build_permission_prompt(prompt))
        notified_user_ids = await self._broadcast(text=text, keyboard=result.keyboard, parse_mode="HTML")
        async with self._state_lock:
            state.notified_user_ids.extend(notified_user_ids)

        logger.info(
            "unbound permission broadcast sent",
            extra={
                "tool_use_id": tool_use_id,
                "session_id": event.session_id,
                "notified_count": len(notified_user_ids),
            },
        )

    async def handle_response(self, *, tool_use_id: str, user_id: int, decision: str) -> UnboundPermissionResponseResult:
        """Process a response from a user.

        Marks the state as responded inside the lock to prevent concurrent
        responses, then forwards the decision outside the lock.  On forwarding
        failure the responded flag is rolled back and the expiry task is
        re-created so the permission will eventually auto-deny rather than
        leaving Claude stuck forever.
        """
        async with self._state_lock:
            state = self._pending.get(tool_use_id)
            if state is None or state.responded:
                return UnboundPermissionResponseResult(accepted=False, forwarded=False)
            state.responded = True
            state.responded_by = user_id
            self._cancel_expiry_task(tool_use_id)

        forwarded = False
        try:
            forwarded = bool(
                await self._hook_socket_server.respond_to_permission(
                    tool_use_id=tool_use_id,
                    decision=decision,
                    reason=f"responded by user {user_id}",
                )
            )
        except Exception:
            logger.warning(
                "failed to forward unbound permission decision",
                extra={"tool_use_id": tool_use_id, "user_id": user_id, "decision": decision},
            )

        if forwarded:
            async with self._state_lock:
                self._pending.pop(tool_use_id, None)
        else:
            async with self._state_lock:
                current = self._pending.get(tool_use_id)
                if current is state:
                    state.responded = False
                    state.responded_by = None
                    elapsed = (utc_now() - state.created_at).total_seconds()
                    remaining = max(0.0, self._permission_ttl_sec - elapsed)
                    self._expiry_tasks[tool_use_id] = asyncio.create_task(
                        self._expire_permission(tool_use_id, remaining_ttl=remaining),
                    )
            if current is not state:
                logger.error(
                    "unbound permission state lost during forwarding (race with invalidate_session), "
                    "sending deny fallback to unblock Claude",
                    extra={"tool_use_id": tool_use_id, "user_id": user_id, "session_id": state.session_id},
                )
                try:
                    await self._hook_socket_server.respond_to_permission(
                        tool_use_id=tool_use_id,
                        decision="deny",
                        reason=f"state lost: forwarding failed after session invalidation (user {user_id})",
                    )
                except Exception:
                    logger.exception(
                        "failed to send deny fallback for lost permission state",
                        extra={"tool_use_id": tool_use_id},
                    )
            else:
                logger.warning(
                    "unbound permission decision not forwarded",
                    extra={"tool_use_id": tool_use_id, "user_id": user_id, "decision": decision},
                )

        logger.info(
            "unbound permission responded",
            extra={
                "tool_use_id": tool_use_id,
                "user_id": user_id,
                "decision": decision,
                "session_id": state.session_id,
                "forwarded": forwarded,
            },
        )
        return UnboundPermissionResponseResult(accepted=forwarded, forwarded=forwarded)

    async def invalidate_session(self, session_id: str) -> int:
        async with self._state_lock:
            tool_use_ids = [tool_use_id for tool_use_id, state in self._pending.items() if state.session_id == session_id]
            for tool_use_id in tool_use_ids:
                self._pending.pop(tool_use_id, None)
                self._cancel_expiry_task(tool_use_id)
            return len(tool_use_ids)

    async def _expire_permission(self, tool_use_id: str, *, remaining_ttl: float | None = None) -> None:
        """Auto-deny on TTL expiry if no response received."""
        delay = remaining_ttl if remaining_ttl is not None else self._permission_ttl_sec
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        session_id: str | None = None
        async with self._state_lock:
            state = self._pending.pop(tool_use_id, None)
            self._expiry_tasks.pop(tool_use_id, None)
            if state is None or state.responded:
                return
            session_id = state.session_id

        await self._hook_socket_server.respond_to_permission(
            tool_use_id=tool_use_id,
            decision="deny",
            reason="no user responded within TTL",
        )

        logger.info(
            "unbound permission expired, auto-denied",
            extra={"tool_use_id": tool_use_id, "session_id": session_id},
        )

    async def is_unbound_permission(self, tool_use_id: str) -> bool:
        """Check if a tool_use_id belongs to an unbound permission request."""
        async with self._state_lock:
            return tool_use_id in self._pending

    async def get_session_id(self, tool_use_id: str) -> str | None:
        """Get the session_id for an unbound permission request."""
        async with self._state_lock:
            state = self._pending.get(tool_use_id)
            return state.session_id if state is not None else None

    async def _broadcast(self, *, text: str, keyboard: Keyboard | None, parse_mode: str | None) -> list[int]:
        notified_user_ids: list[int] = []
        for user_id in sorted(await self._notification_user_ids()):
            try:
                await self._message_sender.send_message(chat_id=user_id, text=text, keyboard=keyboard, parse_mode=parse_mode)
                notified_user_ids.append(user_id)
            except Exception:
                logger.warning(
                    "failed to send unbound permission notification",
                    extra={"user_id": user_id},
                )
        return notified_user_ids

    async def _notification_user_ids(self) -> set[int]:
        user_ids = set(self._allowed_user_ids)
        resolver = self._notify_user_ids_resolver
        if user_ids or resolver is None:
            return user_ids
        return await resolver()

    def _resolve_title(self, session_id: str, cwd: str) -> str | None:
        if self._title_resolver is None:
            return None
        try:
            return self._title_resolver(session_id, cwd)
        except Exception:
            return None

    def _cancel_expiry_task(self, tool_use_id: str) -> None:
        """Cancel an existing expiry task for the given tool_use_id."""
        task = self._expiry_tasks.pop(tool_use_id, None)
        if task is not None:
            task.cancel()
