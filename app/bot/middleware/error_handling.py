from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)


class ErrorHandlingMiddleware(BaseMiddleware):
    """统一错误处理中间件"""

    async def __call__(
        self,
        handler: Callable,
        event: Message | CallbackQuery,
        data: dict,
    ) -> Any:
        try:
            return await handler(event, data)
        except ValueError as exc:
            logger.warning(f"Handler error: {exc}")
            if isinstance(event, Message):
                await event.answer(f"操作失败: {exc}")
            elif isinstance(event, CallbackQuery):
                await event.answer(f"操作失败: {exc}", show_alert=True)
        except Exception:
            logger.exception("Handler exception")
            if isinstance(event, Message):
                await event.answer("发生内部错误，请稍后重试")
            elif isinstance(event, CallbackQuery):
                await event.answer("发生内部错误", show_alert=True)
