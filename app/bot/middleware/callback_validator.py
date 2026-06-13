"""回调数据验证中间件。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery


class CallbackValidatorMiddleware(BaseMiddleware):
    """回调数据验证中间件。

    验证 CallbackQuery.data 的格式是否符合预期，防止非法回调数据到达处理器。

    用法::

        # 验证 "prefix:arg1:arg2" 格式（3 段，前缀为 "prefix"）
        validator = CallbackValidatorMiddleware(expected_parts=3, prefix="prefix")
        router.callback_query.middleware(validator)

        # 仅验证前缀，不限制段数
        validator = CallbackValidatorMiddleware(prefix="prefix")
        router.callback_query.middleware(validator)
    """

    def __init__(
        self,
        expected_parts: int | None = None,
        prefix: str | None = None,
    ) -> None:
        super().__init__()
        self._expected_parts = expected_parts
        self._prefix = prefix

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        if not event.data:
            await event.answer("无效的回调数据", show_alert=True)
            return

        parts = event.data.split(":")

        if self._expected_parts is not None and len(parts) != self._expected_parts:
            await event.answer("无效的回调数据", show_alert=True)
            return

        if self._prefix and not parts[0].startswith(self._prefix):
            await event.answer("无效的回调数据", show_alert=True)
            return

        data["callback_parts"] = tuple(parts)
        return await handler(event, data)
