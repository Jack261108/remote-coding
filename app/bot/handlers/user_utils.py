"""Telegram 用户 ID 提取工具。"""

from __future__ import annotations

from aiogram.types import CallbackQuery, Message


def extract_user_id(obj: Message | CallbackQuery) -> int:
    """从 Message 或 CallbackQuery 中提取 user_id，缺失时返回 0。"""
    return obj.from_user.id if obj.from_user else 0
