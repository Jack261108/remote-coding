"""Telegram 回调处理工具函数。"""

from __future__ import annotations

import logging

from aiogram.types import CallbackQuery

_INSTRUCTION_LINE = "请点击下方按钮选择允许或拒绝。"

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
    """应用回调响应：原地编辑消息（替换提示行+去键盘）、回答回调。"""
    msg = callback.message
    if msg is not None and (edit_text or clear_keyboard):
        try:
            original = msg.text or ""  # type: ignore[union-attr]
            if edit_text and _INSTRUCTION_LINE in original:
                new_text = original.replace(_INSTRUCTION_LINE, edit_text)
                await msg.edit_text(new_text, reply_markup=None)  # type: ignore[union-attr]
            elif edit_text:
                await msg.edit_text(edit_text, reply_markup=None)  # type: ignore[union-attr]
            elif clear_keyboard:
                await msg.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
        except Exception:
            logger.exception("failed to apply %s callback response", log_prefix)
    await callback.answer(alert_text, show_alert=show_alert)
