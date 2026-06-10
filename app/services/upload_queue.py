from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from app.infra.async_utils import cancel_optional_task


@dataclass(frozen=True, slots=True)
class QueuedUpload:
    filename: str
    data: bytes
    size_bytes: int
    created_at: float
    workdir: str | None = None


@dataclass(frozen=True, slots=True)
class UploadQueueEnqueueResult:
    accepted: bool
    reason: str = ""


class UploadQueueManager:
    def __init__(
        self,
        *,
        max_files_per_user: int,
        max_bytes_per_user: int,
        ttl_sec: float = 3600.0,
        cleanup_interval_sec: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_files_per_user < 0:
            raise ValueError("max_files_per_user must be non-negative")
        if max_bytes_per_user < 0:
            raise ValueError("max_bytes_per_user must be non-negative")
        if ttl_sec < 0:
            raise ValueError("ttl_sec must be non-negative")
        if cleanup_interval_sec <= 0:
            raise ValueError("cleanup_interval_sec must be positive")
        self._max_files_per_user = max_files_per_user
        self._max_bytes_per_user = max_bytes_per_user
        self._ttl_sec = ttl_sec
        self._cleanup_interval_sec = cleanup_interval_sec
        self._clock = clock
        self._queues: dict[int, deque[QueuedUpload]] = {}
        self._byte_totals: dict[int, int] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None

    def _prune_expired_locked(self, user_id: int) -> int:
        queue = self._queues.get(user_id)
        if queue is None:
            return 0

        expires_before = self._clock() - self._ttl_sec
        removed_count = 0
        removed_bytes = 0
        while queue and queue[0].created_at <= expires_before:
            removed_count += 1
            removed_bytes += queue.popleft().size_bytes

        if queue:
            self._byte_totals[user_id] = max(0, self._byte_totals.get(user_id, 0) - removed_bytes)
        else:
            self._queues.pop(user_id, None)
            self._byte_totals.pop(user_id, None)
        return removed_count

    async def prune_expired(self) -> int:
        async with self._lock:
            expired = 0
            for user_id in list(self._queues):
                expired += self._prune_expired_locked(user_id)
            return expired

    async def start_cleanup(self) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop_cleanup(self) -> None:
        task = self._cleanup_task
        self._cleanup_task = None
        await cancel_optional_task(task)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval_sec)
            await self.prune_expired()

    async def enqueue(self, *, user_id: int, filename: str, data: bytes, workdir: str | None = None) -> UploadQueueEnqueueResult:
        size_bytes = len(data)
        async with self._lock:
            self._prune_expired_locked(user_id)
            if self._max_files_per_user == 0:
                return UploadQueueEnqueueResult(False, "上传队列已关闭，请等待当前任务完成后重新上传。")

            queue = self._queues.get(user_id)
            queued_files = len(queue) if queue is not None else 0
            if queued_files >= self._max_files_per_user:
                return UploadQueueEnqueueResult(False, f"队列已满，最多允许排队 {self._max_files_per_user} 个文件。")

            current_total = self._byte_totals.get(user_id, 0)
            if current_total + size_bytes > self._max_bytes_per_user:
                return UploadQueueEnqueueResult(
                    False,
                    f"队列容量不足，当前排队 {current_total} 字节，本文件 {size_bytes} 字节，上限 {self._max_bytes_per_user} 字节。",
                )

            if queue is None:
                queue = deque()
                self._queues[user_id] = queue
            queue.append(QueuedUpload(filename=filename, data=data, size_bytes=size_bytes, created_at=self._clock(), workdir=workdir))
            self._byte_totals[user_id] = current_total + size_bytes
            return UploadQueueEnqueueResult(True)

    async def drain(self, *, user_id: int) -> list[QueuedUpload]:
        async with self._lock:
            self._prune_expired_locked(user_id)
            queue = self._queues.pop(user_id, deque())
            self._byte_totals.pop(user_id, None)
            return list(queue)

    async def queued_count(self, *, user_id: int) -> int:
        async with self._lock:
            self._prune_expired_locked(user_id)
            return len(self._queues.get(user_id, ()))
