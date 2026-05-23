from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ref_count: int = 0
    last_used: float = 0.0


class RefCountedLockRegistry:
    def __init__(
        self,
        *,
        ttl_sec: int,
        cleanup_interval_sec: int,
        cleanup_batch_size: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")
        if cleanup_interval_sec <= 0:
            raise ValueError("cleanup_interval_sec must be positive")
        if cleanup_batch_size <= 0:
            raise ValueError("cleanup_batch_size must be positive")
        self._ttl_sec = ttl_sec
        self._cleanup_interval_sec = cleanup_interval_sec
        self._cleanup_batch_size = cleanup_batch_size
        self._clock = clock
        self._entries: dict[str, _LockEntry] = {}
        self._cleanup_queue: deque[str] = deque()
        self._cleanup_queued: set[str] = set()
        self._last_cleanup_ts = 0.0
        self._registry_lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def queued_count(self) -> int:
        return len(self._cleanup_queued)

    @asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        entry = await self._acquire_entry(key)
        try:
            async with entry.lock:
                yield None
        finally:
            await self._release_entry(key, entry)

    async def cleanup_key(self, key: str, *, require_expired: bool = True) -> None:
        async with self._registry_lock:
            self._cleanup_key_locked(key, now=self._now(), require_expired=require_expired)

    async def cleanup_expired(self) -> None:
        async with self._registry_lock:
            self._cleanup_expired_locked(now=self._now())

    async def clear(self) -> None:
        async with self._registry_lock:
            self._entries.clear()
            self._cleanup_queue.clear()
            self._cleanup_queued.clear()

    async def _acquire_entry(self, key: str) -> _LockEntry:
        async with self._registry_lock:
            now = self._now()
            self._cleanup_expired_locked(now=now)
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(last_used=now)
                self._entries[key] = entry
            entry.ref_count += 1
            self._enqueue_locked(key)
            return entry

    async def _release_entry(self, key: str, entry: _LockEntry) -> None:
        async with self._registry_lock:
            current = self._entries.get(key)
            if current is entry:
                now = self._now()
                entry.ref_count = max(0, entry.ref_count - 1)
                entry.last_used = now
                self._cleanup_key_locked(key, now=now, require_expired=True)
                self._cleanup_expired_locked(now=now)

    def _cleanup_expired_locked(self, *, now: float) -> None:
        if now - self._last_cleanup_ts < self._cleanup_interval_sec:
            return
        self._last_cleanup_ts = now
        for _ in range(min(self._cleanup_batch_size, len(self._cleanup_queue))):
            key = self._cleanup_queue.popleft()
            self._cleanup_queued.discard(key)
            entry = self._entries.get(key)
            if entry is None:
                continue
            if self._can_delete_entry(entry, now=now, require_expired=True):
                self._entries.pop(key, None)
                continue
            self._enqueue_locked(key)

    def _cleanup_key_locked(self, key: str, *, now: float, require_expired: bool) -> None:
        entry = self._entries.get(key)
        if entry is None:
            self._cleanup_queued.discard(key)
            return
        if self._can_delete_entry(entry, now=now, require_expired=require_expired):
            self._entries.pop(key, None)
            self._cleanup_queued.discard(key)

    def _can_delete_entry(self, entry: _LockEntry, *, now: float, require_expired: bool) -> bool:
        if entry.ref_count != 0 or entry.lock.locked():
            return False
        if require_expired and now - entry.last_used < self._ttl_sec:
            return False
        return True

    def _enqueue_locked(self, key: str) -> None:
        if key not in self._cleanup_queued:
            self._cleanup_queue.append(key)
            self._cleanup_queued.add(key)

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return asyncio.get_running_loop().time()
