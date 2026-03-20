from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message


class AuthMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: set[int], allow_all_users: bool = False) -> None:
        super().__init__()
        self._allowed_user_ids = allowed_user_ids
        self._allow_all_users = allow_all_users

    async def __call__(
        self,
        handler: Callable[[Message, dict], Awaitable],
        event: Message,
        data: dict,
    ):
        user = event.from_user
        if user is None:
            await event.answer("未授权用户，拒绝访问。")
            return None

        if not self._allow_all_users and user.id not in self._allowed_user_ids:
            await event.answer("未授权用户，拒绝访问。")
            return None

        return await handler(event, data)
