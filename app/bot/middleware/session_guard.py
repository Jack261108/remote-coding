"""会话守卫中间件。

在 handler 执行前检查用户是否已创建会话，可选要求会话处于活跃状态。
不满足条件时向用户回复提示消息并阻止 handler 执行。

核心功能：
- 检查用户是否已通过 ``/session`` 或 ``/claude`` 创建会话。
- 可选要求会话的 ``claude_chat_active`` 为真（用于需要活跃 Claude 会话的命令）。
- 支持跳过特定命令和回调前缀（如 ``/start``、``/session`` 等不需要会话的命令）。
- 通过 ``data["session"]`` 向下游 handler 注入 ``SessionContext`` 对象。

使用方式::

    from app.bot.middleware.session_guard import SessionGuardMiddleware

    # 基础守卫：仅要求会话存在
    guard = SessionGuardMiddleware(session_service, require_active=False)
    router.message.middleware(guard)

    # 活跃守卫：要求会话处于活跃状态
    guard_active = SessionGuardMiddleware(session_service, require_active=True)
    router.message.middleware(guard_active)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.user_utils import extract_user_id
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


class SessionGuardMiddleware(BaseMiddleware):
    """会话守卫中间件。

    Parameters
    ----------
    session_service:
        会话服务实例。
    require_active:
        若为 ``True``，则要求 ``session.claude_chat_active`` 为真才放行。
    skip_commands:
        不需要会话守卫的命令列表（如 ``("/start", "/session", "/claude")``）。
        当消息以这些命令开头时，跳过守卫检查。
    """

    def __init__(
        self,
        session_service: SessionService,
        require_active: bool = False,
        skip_commands: tuple[str, ...] = (),
        skip_callback_prefixes: tuple[str, ...] = (),
    ) -> None:
        """初始化会话守卫中间件。

        Parameters
        ----------
        session_service:
            会话服务实例，用于查询用户会话。
        require_active:
            若为 ``True``，则要求 ``session.claude_chat_active`` 为真才放行。
        skip_commands:
            不需要会话守卫的命令列表（如 ``("/start", "/session", "/claude")``）。
            当消息以这些命令开头时，跳过守卫检查。
        skip_callback_prefixes:
            不需要会话守卫的回调前缀列表。
            当回调数据以这些前缀开头时，跳过守卫检查。
        """
        super().__init__()
        self._session_service = session_service
        self._require_active = require_active
        self._skip_commands = skip_commands
        self._skip_callback_prefixes = skip_callback_prefixes

    async def __call__(
        self,
        handler: Callable[[Any, dict], Awaitable],
        event: Any,
        data: dict,
    ) -> Any:
        """执行守卫检查并放行或拦截 handler。

        检查流程：
        1. 提取用户 ID，无法提取时直接放行。
        2. 检查是否匹配跳过命令或回调前缀。
        3. 查询用户会话，不存在时回复提示并拦截。
        4. 若要求活跃会话，检查 ``claude_chat_active`` 状态。
        5. 将 ``session`` 注入 ``data`` 并放行 handler。

        Parameters
        ----------
        handler:
            下游 handler 函数。
        event:
            aiogram 事件对象。
        data:
            handler 数据字典，验证通过后会注入 ``session`` 键。

        Returns
        -------
        Any
            handler 的返回值，拦截时返回 ``None``。
        """
        user_id = extract_user_id(event)
        if not user_id:
            return await handler(event, data)

        # 跳过不需要会话的命令
        if self._skip_commands and isinstance(event, Message) and event.text:
            text = event.text.strip()
            if any(text == cmd or text.startswith(f"{cmd} ") for cmd in self._skip_commands):
                return await handler(event, data)

        # 跳过不需要会话的回调前缀
        if self._skip_callback_prefixes and isinstance(event, CallbackQuery) and event.data:
            if any(event.data.startswith(prefix) for prefix in self._skip_callback_prefixes):
                return await handler(event, data)

        session = await self._session_service.get(user_id)

        if session is None:
            error_msg = "请先使用 /session 或 /claude 创建会话"
            try:
                if isinstance(event, Message):
                    await event.answer(error_msg)
                elif isinstance(event, CallbackQuery):
                    await event.answer(error_msg, show_alert=True)
            except Exception:
                logger.warning("Failed to send session guard reply", exc_info=True)
            return None

        if self._require_active and not session.claude_chat_active:
            error_msg = "请先发送 /claude 开启会话"
            try:
                if isinstance(event, Message):
                    await event.answer(error_msg)
                elif isinstance(event, CallbackQuery):
                    await event.answer(error_msg, show_alert=True)
            except Exception:
                logger.warning("Failed to send session guard reply", exc_info=True)
            return None

        data["session"] = session
        return await handler(event, data)
