from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from aiogram.exceptions import TelegramBadRequest


SendText = Callable[[str], Awaitable[None]]


class ChunkSender:
    def __init__(self, *, chunk_size: int, flush_interval_sec: float) -> None:
        self._chunk_size = chunk_size
        self._flush_interval_sec = flush_interval_sec
        self._buffer: list[str] = []
        self._buffer_size = 0
        self._lock = asyncio.Lock()
        self._last_flush = 0.0

    async def push(self, text: str, send_fn: SendText) -> None:
        if not text:
            return

        async with self._lock:
            for chunk in self._split(text):
                self._buffer.append(chunk)
                self._buffer_size += len(chunk)
                now = asyncio.get_running_loop().time()
                if self._last_flush == 0.0:
                    self._last_flush = now
                need_flush = self._buffer_size >= self._chunk_size or (now - self._last_flush) >= self._flush_interval_sec
                if need_flush:
                    payload = "".join(self._buffer)
                    self._buffer.clear()
                    self._buffer_size = 0
                    self._last_flush = now
                    await self._safe_send(payload, send_fn)

    async def flush(self, send_fn: SendText) -> None:
        async with self._lock:
            if not self._buffer:
                return
            payload = "".join(self._buffer)
            self._buffer.clear()
            self._buffer_size = 0
            self._last_flush = asyncio.get_running_loop().time()
            await self._safe_send(payload, send_fn)

    def _split(self, text: str) -> list[str]:
        max_len = min(4096, self._chunk_size)
        return [text[i : i + max_len] for i in range(0, len(text), max_len)]

    async def _safe_send(self, payload: str, send_fn: SendText) -> None:
        if not payload:
            return
        for chunk in self._split(payload):
            if not chunk or not chunk.strip():
                continue
            try:
                await send_fn(chunk)
            except TelegramBadRequest as exc:
                lowered = str(exc).lower()
                if "text must be non-empty" in lowered:
                    continue
                if "message is too long" in lowered and len(chunk) > 1:
                    half = max(1, len(chunk) // 2)
                    await self._safe_send(chunk[:half], send_fn)
                    await self._safe_send(chunk[half:], send_fn)
                else:
                    raise
