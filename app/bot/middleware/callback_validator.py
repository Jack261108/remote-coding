from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject


class CallbackValidatorMiddleware(BaseMiddleware):
    """回调数据验证中间件"""

    def __init__(
        self,
        expected_parts: int | tuple[int, ...] | None = None,
        prefix: str | tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        self._expected_parts = expected_parts
        self._prefix = prefix

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        if not event.data:
            await event.answer("无效的回调数据", show_alert=True)
            return None

        parts = event.data.split(":")

        if self._expected_parts is not None:
            expected_parts = self._expected_parts if isinstance(self._expected_parts, tuple) else (self._expected_parts,)
            if len(parts) not in expected_parts:
                await event.answer("无效的回调数据", show_alert=True)
                return None

        if self._prefix:
            prefixes = self._prefix if isinstance(self._prefix, tuple) else (self._prefix,)
            if not any(parts[0].startswith(prefix) for prefix in prefixes):
                await event.answer("无效的回调数据", show_alert=True)
                return None

        data["callback_parts"] = tuple(parts)
        return await handler(event, data)
