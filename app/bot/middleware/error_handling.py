"""统一错误处理中间件。

为 aiogram 消息和回调查询提供统一的异常捕获与用户友好错误回复机制。

该中间件拦截 handler 中抛出的异常，根据异常类型进行分级处理：
- ``ValueError``：记录 warning 级别日志，向用户回复具体的错误描述。
- ``Exception``：记录 exception 级别日志（含完整 traceback），向用户回复通用错误消息。

使用方式::

    from app.bot.middleware.error_handling import ErrorHandlingMiddleware

    router = Router()
    middleware = ErrorHandlingMiddleware()
    router.message.middleware(middleware)
    router.callback_query.middleware(middleware)

注意：``BaseException`` 子类（如 ``KeyboardInterrupt``、``SystemExit``、
``asyncio.CancelledError``）不会被捕获，这是预期行为。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)


def _extract_event_context(event: Any) -> dict[str, Any]:
    """从事件中提取上下文信息用于日志记录。

    Parameters
    ----------
    event:
        aiogram 事件对象（``Message``、``CallbackQuery`` 等）。

    Returns
    -------
    dict[str, Any]
        包含 ``event_type``、``user_id``、``username`` 等字段的上下文字典。
    """
    ctx: dict[str, Any] = {"event_type": type(event).__name__}
    if isinstance(event, (Message, CallbackQuery)):
        user = event.from_user
        if user is not None:
            ctx["user_id"] = user.id
            ctx["username"] = user.username
    return ctx


class ErrorHandlingMiddleware(BaseMiddleware):
    """统一错误处理中间件。

    捕获 handler 中抛出的异常：
    - ValueError: 记录 warning，回复用户友好的错误消息
    - Exception: 记录 exception，回复通用错误消息

    使用方式::

        middleware = ErrorHandlingMiddleware()
        router.message.middleware(middleware)
        router.callback_query.middleware(middleware)
    """

    async def __call__(
        self,
        handler: Callable[[Any, dict], Awaitable],
        event: Any,
        data: dict,
    ) -> Any:
        """执行 handler 并捕获异常。

        Parameters
        ----------
        handler:
            下游 handler 函数。
        event:
            aiogram 事件对象。
        data:
            handler 数据字典。

        Returns
        -------
        Any
            handler 的返回值，异常时返回 ``None``。
        """
        try:
            return await handler(event, data)
        except ValueError as exc:
            logger.warning("Handler error: %s", exc, extra=_extract_event_context(event))
            error_msg = f"操作失败: {exc}"
            if isinstance(event, Message):
                await event.answer(error_msg)
            elif isinstance(event, CallbackQuery):
                await event.answer(error_msg, show_alert=True)
            return None
        except Exception:
            # logger.exception 包含完整 traceback，确保编程错误不会被静默吞掉。
            # 注意：except Exception 不会捕获 BaseException 子类
            # （如 KeyboardInterrupt、SystemExit、asyncio.CancelledError），
            # 这是预期行为。
            logger.exception("Handler exception", extra=_extract_event_context(event))
            error_msg = "发生内部错误，请稍后重试"
            if isinstance(event, Message):
                await event.answer(error_msg)
            elif isinstance(event, CallbackQuery):
                await event.answer(error_msg, show_alert=True)
            return None
