from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(slots=True)
class QueuedUpload:
    filename: str
    data: bytes
    size: int


class UploadQueueManager:
    """Per-user FIFO upload queue with count and byte limits.

    All state is in-memory only; queued uploads are lost on restart.
    """

    def __init__(self, *, max_files_per_user: int, max_bytes_per_user: int) -> None:
        self._max_files = max_files_per_user
        self._max_bytes = max_bytes_per_user
        self._queues: dict[int, deque[QueuedUpload]] = {}
        self._byte_totals: dict[int, int] = {}

    def enqueue(self, user_id: int, filename: str, data: bytes, size: int) -> tuple[bool, str]:
        """Attempt to enqueue a file for the given user.

        Returns (True, message) on success or (False, reason) on rejection.
        """
        if self._max_files <= 0:
            return (False, "queuing disabled")

        queue = self._queues.get(user_id)
        current_count = len(queue) if queue else 0
        current_bytes = self._byte_totals.get(user_id, 0)

        if current_count >= self._max_files:
            return (False, f"queue full ({self._max_files} files)")

        if current_bytes + size > self._max_bytes:
            return (False, "queue byte limit exceeded")

        if queue is None:
            queue = deque()
            self._queues[user_id] = queue

        queue.append(QueuedUpload(filename=filename, data=data, size=size))
        self._byte_totals[user_id] = current_bytes + size
        return (True, "queued")

    def drain(self, user_id: int) -> list[QueuedUpload]:
        """Pop all queued entries for the user and return them in FIFO order."""
        queue = self._queues.pop(user_id, None)
        self._byte_totals.pop(user_id, None)
        if queue is None:
            return []
        return list(queue)

    def is_full(self, user_id: int) -> bool:
        """Check whether the user's queue is at its file count limit."""
        if self._max_files <= 0:
            return True
        queue = self._queues.get(user_id)
        if queue is None:
            return False
        return len(queue) >= self._max_files
