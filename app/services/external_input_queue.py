"""Per-session FIFO queue for external input while Claude is busy.

When the bound Claude session is in a non-sendable phase (``processing`` /
``compacting`` / ``waiting_for_approval`` / active ``AskUserQuestion``), the
input service enqueues arriving Telegram text instead of rejecting it (design
§9). This module owns only the queue mechanics:

  * FIFO per session bound by ``max_size`` (default 5) — overflowing an enqueue
    refuses the new item with ``QueueEnqueueOverflow`` so the service can tell
    the user to slow down, and reports how many were already queued.
  * Each ``QueuedInput`` carries the ``binding_id`` at enqueue time. ``dequeue``
    drops entries whose ``binding_id`` no longer matches the live generation
    (ABA barrier): a stale entry from before an unbind+rebind is discarded,
    never injected into the new binding.
  * Per-entry TTL: an entry older than ``ttl_sec`` is dropped on the next
    enqueue/dequeue/prune instead of being injected as stale input.
  * ``clear`` returns the dropped entries so the service can report how many
    unsent messages were abandoned (unbind / SessionEnd / target failure).

Concurrency: send serialisation is enforced by the input service's per-session
lock; this queue guards only its own dict with an ``asyncio.Lock``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class QueuedInput:
    """A single enqueued Telegram text, with the binding generation at enqueue."""

    text: str
    enqueued_at: datetime
    binding_id: str


@dataclass(frozen=True, slots=True)
class QueueEnqueueOk:
    """The entry was appended; ``size`` is the queue length after enqueue."""

    size: int


@dataclass(frozen=True, slots=True)
class QueueEnqueueOverflow:
    """The queue is full; the entry was refused. ``size`` is the cap reached."""

    size: int


QueueEnqueueResult = QueueEnqueueOk | QueueEnqueueOverflow


class ExternalInputQueue:
    """In-memory per-session FIFO queue for external input text."""

    def __init__(
        self,
        *,
        max_size: int = 5,
        ttl_sec: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")
        self._max_size = max_size
        self._ttl_sec = ttl_sec
        self._now = now or (lambda: datetime.now(UTC))
        self._queues: dict[str, deque[QueuedInput]] = {}
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        session_id: str,
        *,
        text: str,
        binding_id: str,
    ) -> QueueEnqueueResult:
        """Append ``text`` to the session's queue if there is room.

        Expired entries (older than ttl) are pruned first so a queue that
        sat full during a long busy period can still accept fresh input.
        """
        async with self._lock:
            queue = self._queues.get(session_id)
            if queue is None:
                queue = deque()
                self._queues[session_id] = queue
            self._prune_expired_locked(queue)
            if len(queue) >= self._max_size:
                return QueueEnqueueOverflow(size=len(queue))
            queue.append(QueuedInput(text=text, enqueued_at=self._now(), binding_id=binding_id))
            return QueueEnqueueOk(size=len(queue))

    async def dequeue(
        self,
        session_id: str,
        *,
        binding_id: str,
    ) -> QueuedInput | None:
        """Pop the next injectable entry for ``session_id``.

        Skips and discards expired entries and entries whose ``binding_id``
        does not match the live generation (ABA). Returns ``None`` when no
        live, same-generation entry remains.
        """
        async with self._lock:
            queue = self._queues.get(session_id)
            if queue is None:
                return None
            while queue:
                entry = queue[0]
                if self._is_expired(entry):
                    queue.popleft()
                    continue
                if entry.binding_id != binding_id:
                    # Stale generation: drop and keep draining.
                    queue.popleft()
                    continue
                queue.popleft()
                return entry
            self._maybe_drop_empty(session_id)
            return None

    async def peek_size(self, session_id: str) -> int:
        """Return the number of entries (incl. expired/stale until pruned)."""
        async with self._lock:
            queue = self._queues.get(session_id)
            return len(queue) if queue is not None else 0

    async def clear(self, session_id: str) -> list[QueuedInput]:
        """Drop the whole queue for ``session_id`` and return the discarded
        entries (for an "N unsent messages discarded" notification)."""
        async with self._lock:
            queue = self._queues.pop(session_id, None)
            return list(queue) if queue is not None else []

    async def prune_expired(self, session_id: str) -> int:
        """Drop only expired entries for ``session_id``; return the drop count.

        Used by the drain task to age out entries without clearing same-session
        same-generation pending input.
        """
        async with self._lock:
            queue = self._queues.get(session_id)
            if queue is None:
                return 0
            before = len(queue)
            self._prune_expired_locked(queue)
            self._maybe_drop_empty(session_id)
            return before - len(queue)

    def _prune_expired_locked(self, queue: deque[QueuedInput]) -> None:
        cutoff = self._now()
        # deque has no dropwhile; pop from left while head is expired.
        # Note: prune only the head end (FIFO order means expired heads are old);
        # a middle entry can only become expired while waiting behind a non-expired
        # head, which cannot happen because we always check the head first.
        while queue:
            if cutoff.timestamp() - queue[0].enqueued_at.timestamp() > self._ttl_sec:
                queue.popleft()
                continue
            break

    def _is_expired(self, entry: QueuedInput) -> bool:
        return self._now().timestamp() - entry.enqueued_at.timestamp() > self._ttl_sec

    def _maybe_drop_empty(self, session_id: str) -> None:
        queue = self._queues.get(session_id)
        if queue is not None and not queue:
            self._queues.pop(session_id, None)
