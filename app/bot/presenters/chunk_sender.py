from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from aiogram.exceptions import TelegramBadRequest

from app.bot.presenters.telegram_formatting import split_markdownish_for_telegram


SendText = Callable[[str], Awaitable[bool | None]]


class ChunkSender:
    def __init__(self, *, chunk_size: int, flush_interval_sec: float) -> None:
        self._chunk_size = chunk_size
        self._flush_interval_sec = flush_interval_sec
        self._buffer: list[str] = []
        self._buffer_size = 0
        self._lock = asyncio.Lock()
        self._last_flush = 0.0
        self._pending_flush_task: asyncio.Task | None = None

    async def push(self, text: str, send_fn: SendText) -> bool:
        if not text:
            return True

        payload_to_send = ""
        async with self._lock:
            for chunk in self._split(text):
                self._buffer.append(chunk)
                self._buffer_size += len(chunk)

            now = asyncio.get_running_loop().time()
            if self._last_flush == 0.0:
                self._last_flush = now

            if self._buffer_size >= self._chunk_size:
                payload_to_send = self._drain_buffer_locked(now)
                self._cancel_pending_flush_locked()
            elif self._pending_flush_task is None and self._buffer:
                self._pending_flush_task = asyncio.create_task(self._delayed_flush(send_fn))

        if payload_to_send:
            return await self._safe_send(payload_to_send, send_fn)
        return True

    async def flush(self, send_fn: SendText) -> bool:
        payload = ""
        pending_task: asyncio.Task | None = None
        async with self._lock:
            pending_task = self._pending_flush_task
            self._pending_flush_task = None
            if self._buffer:
                payload = self._drain_buffer_locked(asyncio.get_running_loop().time())

        if pending_task is not None:
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass

        if payload:
            return await self._safe_send(payload, send_fn)
        return True

    async def _delayed_flush(self, send_fn: SendText) -> None:
        try:
            await asyncio.sleep(self._flush_interval_sec)
            payload = ""
            async with self._lock:
                if self._pending_flush_task is not asyncio.current_task():
                    return
                self._pending_flush_task = None
                if self._buffer:
                    payload = self._drain_buffer_locked(asyncio.get_running_loop().time())
            if payload:
                await self._safe_send(payload, send_fn)
        except asyncio.CancelledError:
            raise

    def _drain_buffer_locked(self, now: float) -> str:
        payload = "".join(self._buffer)
        self._buffer.clear()
        self._buffer_size = 0
        self._last_flush = now
        return payload

    def _cancel_pending_flush_locked(self) -> None:
        if self._pending_flush_task is None:
            return
        self._pending_flush_task.cancel()
        self._pending_flush_task = None

    def _split(self, text: str) -> list[str]:
        max_len = min(4096, self._chunk_size)
        return split_markdownish_for_telegram(text, max_len)

    async def _safe_send(self, payload: str, send_fn: SendText) -> bool:
        if not payload:
            return True
        delivered = True
        for chunk in self._split(payload):
            if not chunk or not chunk.strip():
                continue
            try:
                if await send_fn(chunk) is False:
                    delivered = False
            except TelegramBadRequest as exc:
                lowered = str(exc).lower()
                if "text must be non-empty" in lowered:
                    delivered = False
                    continue
                if "message is too long" in lowered and len(chunk) > 1:
                    half = max(1, len(chunk) // 2)
                    left_delivered = await self._safe_send(chunk[:half], send_fn)
                    right_delivered = await self._safe_send(chunk[half:], send_fn)
                    delivered = delivered and left_delivered and right_delivered
                else:
                    raise
        return delivered
