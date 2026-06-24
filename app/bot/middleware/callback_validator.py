"""回调数据验证中间件。

验证 Telegram 回调查询的 ``data`` 字段是否符合预期格式，
防止非法或格式错误的回调数据到达 handler。

验证规则：
- 回调数据按 ``:`` 拆分后的段数必须在 ``expected_parts`` 范围内。
- 首段必须以 ``prefix`` 指定的前缀开头（可选）。
- 验证通过后，将拆分结果以 ``data["callback_parts"]`` 注入 handler 数据。

使用方式::

    from app.bot.middleware.callback_validator import CallbackValidatorMiddleware

    # 验证回调数据格式：3 段，首段以 "sess" 开头
    validator = CallbackValidatorMiddleware(expected_parts=3, prefix="sess")
    router.callback_query.middleware(validator)

    # 支持多个可接受的段数和前缀
    validator = CallbackValidatorMiddleware(
        expected_parts=(4, 5), prefix=("ask", "perm"),
    )
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from app.bot.handlers.callback_utils import parse_callback_prefix

logger = logging.getLogger(__name__)


class CallbackValidatorMiddleware(BaseMiddleware):
    """回调数据验证中间件。

    Parameters
    ----------
    expected_parts:
        callback data 按 ':' 拆分后期望的段数。
        可以是单个整数或可接受的段数元组。
    prefix:
        可选，首段必须以此前缀开头。
        可以是单个字符串或可接受的前缀元组。
    """

    def __init__(
        self,
        expected_parts: int | tuple[int, ...] | None = None,
        prefix: str | tuple[str, ...] | None = None,
    ) -> None:
        """初始化回调数据验证中间件。

        Parameters
        ----------
        expected_parts:
            回调数据按 ``:`` 拆分后期望的段数。
            可以是单个整数、可接受的段数元组，或 ``None`` 表示不校验段数。
        prefix:
            可选，首段必须以此前缀开头。
            可以是单个字符串或可接受的前缀元组。
        """
        super().__init__()
        if expected_parts is None:
            self._expected_parts: tuple[int, ...] | None = None
        else:
            self._expected_parts = expected_parts if isinstance(expected_parts, tuple) else (expected_parts,)
        self._prefix = prefix

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict], Awaitable],
        event: TelegramObject,
        data: dict,
    ) -> Any:
        """验证回调数据格式并放行或拦截 handler。

        验证流程：
        1. 非 ``CallbackQuery`` 事件直接放行。
        2. 回调数据为空时回复错误并拦截。
        3. 按 ``:`` 拆分后检查段数是否在 ``expected_parts`` 范围内。
        4. 若设置了 ``prefix``，检查首段是否以指定前缀开头。
        5. 将拆分结果以 ``callback_parts`` 注入 ``data`` 并放行 handler。

        Parameters
        ----------
        handler:
            下游 handler 函数。
        event:
            aiogram 事件对象。
        data:
            handler 数据字典，验证通过后会注入 ``callback_parts`` 键。

        Returns
        -------
        Any
            handler 的返回值，验证失败时返回 ``None``。
        """
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)
        if not event.data:
            await event.answer("无效的回调数据", show_alert=True)
            return None

        parts: tuple[str, ...] | None = None
        expected_parts = self._expected_parts or (len(event.data.split(":")),)
        prefixes = (self._prefix,) if isinstance(self._prefix, str) else self._prefix

        if prefixes:
            for expected_part in expected_parts:
                for prefix in prefixes:
                    parts = parse_callback_prefix(event.data, expected_part, prefix)
                    if parts is not None:
                        break
                if parts is not None:
                    break
        else:
            candidate_parts = tuple(event.data.split(":"))
            if len(candidate_parts) in expected_parts:
                parts = candidate_parts

        if parts is None:
            await event.answer("无效的回调数据", show_alert=True)
            return None

        data["callback_parts"] = parts
        return await handler(event, data)
