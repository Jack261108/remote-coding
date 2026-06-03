"""Telegram 回调处理工具函数。"""

from __future__ import annotations

import logging

from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)


async def apply_callback_response(
    callback: CallbackQuery,
    edit_text: str | None = None,
    clear_keyboard: bool = False,
    alert_text: str | None = None,
    show_alert: bool = False,
    *,
    log_prefix: str = "",
) -> None:
    """应用回调响应：编辑消息、清除键盘、回答回调。"""
    if callback.message is not None:
        if edit_text:
            try:
                await callback.message.edit_text(edit_text)
            except Exception:
                logger.exception("failed to edit %s callback message", log_prefix)
        if clear_keyboard:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("failed to clear %s inline keyboard", log_prefix)
    await callback.answer(alert_text, show_alert=show_alert)
