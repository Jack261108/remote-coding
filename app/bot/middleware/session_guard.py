from collections.abc import Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class SessionGuardMiddleware(BaseMiddleware):
    """会话守卫中间件"""

    def __init__(self, session_service, require_active: bool = False):
        self._session_service = session_service
        self._require_active = require_active

    async def __call__(
        self,
        handler: Callable,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = data.get("user_id")
        if not user_id:
            return await handler(event, data)

        session = await self._session_service.get(user_id)

        if session is None:
            error_msg = "请先使用 /session 或 /claude 创建会话"
            if isinstance(event, Message):
                await event.answer(error_msg)
            elif isinstance(event, CallbackQuery):
                await event.answer(error_msg, show_alert=True)
            return None

        if self._require_active and not session.claude_chat_active:
            error_msg = "请先发送 /claude 开启会话"
            if isinstance(event, Message):
                await event.answer(error_msg)
            elif isinstance(event, CallbackQuery):
                await event.answer(error_msg, show_alert=True)
            return None

        data["session"] = session
        return await handler(event, data)
