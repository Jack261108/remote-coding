from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.permission_callback_registry import PermissionCallbackRegistry

if TYPE_CHECKING:
    from aiogram import Bot

    from app.domain.session_models import SessionPhase
    from app.domain.user_question_models import UserQuestionPrompt
    from app.services.external_binding_store import ExternalBindingStore

logger = logging.getLogger(__name__)


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse_mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class ExternalSessionPushNotifier:
    """Sends Telegram push notifications for bound external session events."""

    def __init__(
        self,
        *,
        bot: Bot,
        binding_store: ExternalBindingStore,
        retry_count: int = 1,
        permission_callback_registry: PermissionCallbackRegistry | None = None,
    ) -> None:
        self._bot = bot
        self._binding_store = binding_store
        self._retry_count = retry_count
        # Use `is None` rather than `or` because PermissionCallbackRegistry defines
        # __len__, so an empty (newly-constructed) registry is falsy.
        self._permission_callback_registry = (
            permission_callback_registry if permission_callback_registry is not None else PermissionCallbackRegistry(ttl_sec=600)
        )

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
        short_id = session_id[:8]
        header = f"🔐 [{title or short_id}] 请求权限: {tool_name}"

        # Build structured message with tool_input details in code block
        lines = [header]
        if tool_input:
            # Show command or file_path in a code block for readability
            command = tool_input.get("command")
            file_path = tool_input.get("file_path") or tool_input.get("path")
            description = tool_input.get("description")
            if command:
                # Truncate very long commands
                cmd_display = command if len(command) <= 300 else command[:300] + "..."
                lines.append(f"\n<code>{_escape_html(cmd_display)}</code>")
            elif file_path:
                lines.append(f"\n<code>{_escape_html(file_path)}</code>")
            if description:
                desc_display = description if len(description) <= 150 else description[:150] + "..."
                lines.append(f"📝 {_escape_html(desc_display)}")
        lines.append(f"📂 {_escape_html(cwd)}")
        text = "\n".join(lines)

        # Register tool_use_id via registry to get a short token
        token = self._permission_callback_registry.register(tool_use_id)
        logger.info(
            "push notifier registered token=%s for tool_use_id=%s registry_id=%s",
            token,
            tool_use_id[:20],
            id(self._permission_callback_registry),
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"ext_perm:{token}:allow"),
                    InlineKeyboardButton(text="❌ Deny", callback_data=f"ext_perm:{token}:deny"),
                ],
                [
                    InlineKeyboardButton(text="🟢 Auto-approve All", callback_data=f"ext_perm:{token}:auto_approve"),
                ],
            ]
        )
        return await self._send_with_retry(chat_id=user_id, text=text, reply_markup=keyboard, parse_mode="HTML")

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
            buttons: list[list[InlineKeyboardButton]] = []
            tool_use_id = prompt.tool_use_id
            for i, option in enumerate(prompt.options):
                # Telegram callback_data max 64 bytes; truncate tool_use_id if needed
                cb_data = f"ext_uq:{tool_use_id}:{i}"
                if len(cb_data.encode()) > 64:
                    # Truncate tool_use_id to fit
                    max_id_len = 64 - len(f"ext_uq::{i}".encode())
                    cb_data = f"ext_uq:{tool_use_id[:max_id_len]}:{i}"
                buttons.append([InlineKeyboardButton(text=f"{i + 1}. {option.label}"[:40], callback_data=cb_data)])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            return await self._send_with_retry(chat_id=user_id, text=text, reply_markup=keyboard)
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

    async def _send_with_retry(
        self, *, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = None
    ) -> bool:
        """Send message with retry on failure."""
        for attempt in range(1 + self._retry_count):
            try:
                await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
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
