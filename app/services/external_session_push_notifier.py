from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:
    from aiogram import Bot

    from app.domain.session_models import SessionPhase
    from app.services.external_binding_store import ExternalBindingStore

logger = logging.getLogger(__name__)


class ExternalSessionPushNotifier:
    """Sends Telegram push notifications for bound external session events."""

    def __init__(
        self,
        *,
        bot: Bot,
        binding_store: ExternalBindingStore,
        retry_count: int = 1,
    ) -> None:
        self._bot = bot
        self._binding_store = binding_store
        self._retry_count = retry_count

    async def notify_permission_request(
        self,
        *,
        user_id: int,
        session_id: str,
        tool_name: str,
        tool_input: dict | None,
        tool_use_id: str,
        cwd: str,
    ) -> bool:
        """Send permission request notification to bound user. Returns True if delivered."""
        short_id = session_id[:8]
        text = f"🔐 [{short_id}] 请求权限: {tool_name}\n路径: {cwd}"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"ext_perm:{tool_use_id}:allow"),
                    InlineKeyboardButton(text="❌ Deny", callback_data=f"ext_perm:{tool_use_id}:deny"),
                ]
            ]
        )
        return await self._send_with_retry(chat_id=user_id, text=text, reply_markup=keyboard)

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

    async def notify_info(
        self,
        *,
        user_id: int,
        text: str,
    ) -> bool:
        """Send an informational notification (no action buttons). Returns True if delivered."""
        return await self._send_with_retry(chat_id=user_id, text=text)

    async def _send_with_retry(self, *, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
        """Send message with retry on failure."""
        for attempt in range(1 + self._retry_count):
            try:
                await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
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
