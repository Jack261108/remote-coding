from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from app.bot.handlers.callback_utils import parse_callback_prefix


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

        raw_parts = tuple(event.data.split(":"))
        parts: tuple[str, ...] | None = None

        if self._expected_parts is None:
            parts = raw_parts
        elif self._prefix is None:
            expected_parts = self._expected_parts if isinstance(self._expected_parts, tuple) else (self._expected_parts,)
            if len(raw_parts) in expected_parts:
                parts = raw_parts
        else:
            expected_parts = self._expected_parts if isinstance(self._expected_parts, tuple) else (self._expected_parts,)
            prefixes = self._prefix if isinstance(self._prefix, tuple) else (self._prefix,)
            for expected_part in expected_parts:
                for prefix in prefixes:
                    parts = parse_callback_prefix(event.data, expected_part, prefix)
                    if parts is not None:
                        break
                if parts is not None:
                    break

        if parts is None or (
            self._prefix is not None
            and not any(
                raw_parts[0].startswith(prefix) for prefix in (self._prefix if isinstance(self._prefix, tuple) else (self._prefix,))
            )
        ):
            await event.answer("无效的回调数据", show_alert=True)
            return None

        data["callback_parts"] = parts
        return await handler(event, data)
