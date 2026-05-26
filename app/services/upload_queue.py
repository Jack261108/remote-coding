from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueuedUpload:
    filename: str
    data: bytes
    size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadQueueEnqueueResult:
    accepted: bool
    reason: str = ""


class UploadQueueManager:
    def __init__(self, *, max_files_per_user: int, max_bytes_per_user: int) -> None:
        if max_files_per_user < 0:
            raise ValueError("max_files_per_user must be non-negative")
        if max_bytes_per_user < 0:
            raise ValueError("max_bytes_per_user must be non-negative")
        self._max_files_per_user = max_files_per_user
        self._max_bytes_per_user = max_bytes_per_user
        self._queues: dict[int, deque[QueuedUpload]] = {}
        self._byte_totals: dict[int, int] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, *, user_id: int, filename: str, data: bytes) -> UploadQueueEnqueueResult:
        size_bytes = len(data)
        async with self._lock:
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
            queue.append(QueuedUpload(filename=filename, data=data, size_bytes=size_bytes))
            self._byte_totals[user_id] = current_total + size_bytes
            return UploadQueueEnqueueResult(True)

    async def drain(self, *, user_id: int) -> list[QueuedUpload]:
        async with self._lock:
            queue = self._queues.pop(user_id, deque())
            self._byte_totals.pop(user_id, None)
            return list(queue)

    async def queued_count(self, *, user_id: int) -> int:
        async with self._lock:
            return len(self._queues.get(user_id, ()))
