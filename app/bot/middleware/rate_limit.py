from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, *, limit: int, window_sec: int) -> None:
        super().__init__()
        self._limit = limit
        self._window_sec = window_sec
        self._buckets: dict[int, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        handler: Callable[[Message, dict], Awaitable],
        event: Message,
        data: dict,
    ):
        user = event.from_user
        if user is None:
            return await handler(event, data)

        now = asyncio.get_running_loop().time()
        async with self._lock:
            bucket = self._buckets.setdefault(user.id, deque())
            while bucket and now - bucket[0] > self._window_sec:
                bucket.popleft()

            if len(bucket) >= self._limit:
                await event.answer("请求过于频繁，请稍后再试。")
                return None

            bucket.append(now)

        return await handler(event, data)
