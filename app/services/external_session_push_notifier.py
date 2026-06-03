from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.domain.permission_models import PermissionPromptInput
from app.infra.text_formatting import render_markdownish_to_telegram_html
from app.services.message_sender import Button, Keyboard, MessageSender
from app.services.permission_callback_registry import SessionOrigin
from app.services.permission_gateway import RegisterForButtonConflict, RegisterForButtonOk

if TYPE_CHECKING:
    from app.domain.session_models import SessionPhase
    from app.domain.user_question_models import UserQuestionPrompt
    from app.services.external_binding_store import ExternalBindingStore
    from app.services.permission_gateway import PermissionGateway

logger = logging.getLogger(__name__)


class ExternalSessionPushNotifier:
    """Sends Telegram push notifications for bound external session events."""

    def __init__(
        self,
        *,
        message_sender: MessageSender,
        binding_store: ExternalBindingStore,
        retry_count: int = 1,
        permission_gateway: PermissionGateway | None = None,
    ) -> None:
        self._message_sender = message_sender
        self._binding_store = binding_store
        self._retry_count = retry_count
        self._permission_gateway = permission_gateway

    async def notify_permission_request(
        self,
        *,
        user_id: int,
        session_id: str,
        tool_name: str,
        tool_input: dict | None,
        tool_use_id: str,
        cwd: str,
        title: str | None = None,
    ) -> bool:
        """Send permission request notification to bound user. Returns True if delivered."""
        gateway = self._permission_gateway
        if gateway is None:
            raise RuntimeError("permission gateway is not configured")

        result = await gateway.register_for_button(
            tool_use_id=tool_use_id,
            session_id=session_id,
            origin=SessionOrigin.EXTERNAL_BOUND,
            candidate_user_id=user_id,
        )
        if isinstance(result, RegisterForButtonConflict):
            logger.warning(
                "bound permission registration conflict",
                extra={"tool_use_id": tool_use_id, "session_id": session_id, "user_id": user_id},
            )
            return await self._send_with_retry(chat_id=user_id, text=result.advisory_text, keyboard=result.keyboard)
        if not isinstance(result, RegisterForButtonOk):
            raise RuntimeError("unexpected permission gateway registration result")

        prompt = PermissionPromptInput(
            tool_name=tool_name or "unknown tool",
            tool_input=tool_input,
            cwd=cwd,
            session_id=session_id,
            session_title=title,
        )
        text = render_markdownish_to_telegram_html(gateway.message_builder.build_permission_prompt(prompt))
        return await self._send_with_retry(chat_id=user_id, text=text, keyboard=result.keyboard, parse_mode="HTML")

    async def notify_permission_resolved_in_terminal(
        self,
        *,
        user_id: int,
        session_id: str,
        tool_name: str,
        tool_use_id: str,
        reason: str,
    ) -> bool:
        """Notify user that a permission was resolved in the terminal."""
        short_id = session_id[:8]
        reason_text = "已批准" if reason == "terminal_approved" else reason
        text = f"✅ [{short_id}] 权限已在终端{reason_text}\n工具: {tool_name}"
        return await self._send_with_retry(chat_id=user_id, text=text)

    async def notify_phase_change(
        self,
        *,
        user_id: int,
        session_id: str,
        old_phase: SessionPhase,
        new_phase: SessionPhase,
        cwd: str,
    ) -> bool:
        """Send phase change notification. Returns True if delivered."""
        short_id = session_id[:8]
        text = f"📊 [{short_id}] {old_phase.value} → {new_phase.value}\n路径: {cwd}"
        return await self._send_with_retry(chat_id=user_id, text=text)

    async def notify_session_end(
        self,
        *,
        user_id: int,
        session_id: str,
        cwd: str,
    ) -> bool:
        """Send session ended notification. Returns True if delivered."""
        short_id = session_id[:8]
        text = f"🔚 [{short_id}] 会话已结束\n路径: {cwd}"
        return await self._send_with_retry(chat_id=user_id, text=text)

    async def notify_user_question(
        self,
        *,
        user_id: int,
        session_id: str,
        prompts: tuple[UserQuestionPrompt, ...],
        interactive: bool = False,
    ) -> bool:
        """Send notification showing AskUserQuestion options.

        When *interactive* is True, options are shown as clickable buttons that
        inject the answer into the external terminal via PTY injection.
        Otherwise, this is informational only (user answers in terminal).
        Returns True if delivered.
        """
        if not prompts:
            return False
        short_id = session_id[:8]
        # For interactive mode, we only show the first unanswered prompt with buttons
        prompt = prompts[0]
        lines: list[str] = []
        lines.append(f"❓ [{short_id}] 用户选择")
        lines.append(f"问题: {prompt.question}")
        if prompt.options:
            lines.append("选项:")
            for i, option in enumerate(prompt.options, start=1):
                label = option.label
                if option.description:
                    label += f" — {option.description}"
                lines.append(f"  {i}. {label}")

        if interactive and prompt.options:
            lines.append("")
            lines.append("👇 点击按钮选择:")
            text = "\n".join(lines).rstrip()
            # Build option buttons
            # Callback data format: ext_uq:{tool_use_id}:{option_index}
            buttons: list[list[Button]] = []
            tool_use_id = prompt.tool_use_id
            for i, option in enumerate(prompt.options):
                # Telegram callback_data max 64 bytes; truncate tool_use_id if needed
                cb_data = f"ext_uq:{tool_use_id}:{i}"
                if len(cb_data.encode()) > 64:
                    # Truncate tool_use_id to fit
                    max_id_len = 64 - len(f"ext_uq::{i}".encode())
                    cb_data = f"ext_uq:{tool_use_id[:max_id_len]}:{i}"
                buttons.append([Button(text=f"{i + 1}. {option.label}"[:40], callback_data=cb_data)])
            keyboard = Keyboard(rows=buttons)
            return await self._send_with_retry(chat_id=user_id, text=text, keyboard=keyboard)
        else:
            lines.append("请在终端中选择")
            lines.append("")
            text = "\n".join(lines).rstrip()
            return await self._send_with_retry(chat_id=user_id, text=text)

    async def notify_info(
        self,
        *,
        user_id: int,
        text: str,
    ) -> bool:
        """Send an informational notification (no action buttons). Returns True if delivered."""
        return await self._send_with_retry(chat_id=user_id, text=text)

    async def _send_with_retry(self, *, chat_id: int, text: str, keyboard: Keyboard | None = None, parse_mode: str | None = None) -> bool:
        """Send message with retry on failure."""
        for attempt in range(1 + self._retry_count):
            try:
                await self._message_sender.send_message(chat_id=chat_id, text=text, keyboard=keyboard, parse_mode=parse_mode)
                return True
            except Exception:
                if attempt < self._retry_count:
                    logger.warning(
                        "Push notification delivery failed (attempt %d), retrying...",
                        attempt + 1,
                    )
                else:
                    logger.error(
                        "Push notification delivery failed after %d attempts, giving up. chat_id=%d",
                        attempt + 1,
                        chat_id,
                    )
        return False
