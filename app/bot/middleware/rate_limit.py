from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware


class RateLimitMiddleware(BaseMiddleware):
    def __init__(
        self,
        *,
        limit: int,
        window_sec: int,
        bucket_ttl_sec: int | None = None,
        cleanup_interval_sec: int = 60,
        cleanup_batch_size: int = 50,
    ) -> None:
        super().__init__()
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        effective_bucket_ttl_sec = bucket_ttl_sec if bucket_ttl_sec is not None else window_sec
        if effective_bucket_ttl_sec <= 0:
            raise ValueError("bucket_ttl_sec must be positive")
        if effective_bucket_ttl_sec < window_sec:
            raise ValueError("bucket_ttl_sec must be greater than or equal to window_sec")
        if cleanup_interval_sec <= 0:
            raise ValueError("cleanup_interval_sec must be positive")
        if cleanup_batch_size <= 0:
            raise ValueError("cleanup_batch_size must be positive")
        self._limit = limit
        self._window_sec = window_sec
        self._bucket_ttl_sec = effective_bucket_ttl_sec
        self._cleanup_interval_sec = cleanup_interval_sec
        self._cleanup_batch_size = cleanup_batch_size
        self._buckets: dict[int, deque[float]] = {}
        self._lock = asyncio.Lock()
        self._cleanup_queue: deque[int] = deque()
        self._cleanup_queued: set[int] = set()
        self._last_cleanup_ts: float = 0.0

    def _enqueue_cleanup(self, user_id: int) -> None:
        if user_id not in self._cleanup_queued:
            self._cleanup_queue.append(user_id)
            self._cleanup_queued.add(user_id)

    def _try_cleanup_stale_buckets(self, now: float) -> None:
        if now - self._last_cleanup_ts < self._cleanup_interval_sec:
            return
        self._last_cleanup_ts = now
        for _ in range(min(self._cleanup_batch_size, len(self._cleanup_queue))):
            uid = self._cleanup_queue.popleft()
            self._cleanup_queued.discard(uid)
            bucket = self._buckets.get(uid)
            if bucket is None:
                continue
            if not bucket or now - bucket[-1] > self._bucket_ttl_sec:
                self._buckets.pop(uid, None)
            else:
                self._enqueue_cleanup(uid)

    async def __call__(
        self,
        handler: Callable[[Any, dict], Awaitable],
        event: Any,
        data: dict,
    ):
        user = event.from_user
        if user is None:
            return await handler(event, data)

        now = asyncio.get_running_loop().time()
        limited = False
        async with self._lock:
            self._try_cleanup_stale_buckets(now)

            bucket = self._buckets.get(user.id)
            if bucket is None:
                bucket = deque()
                self._buckets[user.id] = bucket
                self._enqueue_cleanup(user.id)
            elif user.id not in self._cleanup_queued:
                self._enqueue_cleanup(user.id)
            while bucket and now - bucket[0] > self._window_sec:
                bucket.popleft()

            if len(bucket) >= self._limit:
                limited = True
            else:
                bucket.append(now)

        if limited:
            await event.answer("请求过于频繁，请稍后再试。")
            return None

        return await handler(event, data)
