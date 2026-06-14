from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timedelta
from heapq import nsmallest
from typing import Protocol

from app.domain.models import SessionContext, TaskRecord, utc_now


class MemoryTaskStore:
    def __init__(self, max_records: int = 1000, ttl_hours: int = 168) -> None:
        if max_records <= 0:
            raise ValueError(f"max_records must be positive, got {max_records}")
        if ttl_hours <= 0:
            raise ValueError(f"ttl_hours must be positive, got {ttl_hours}")
        self._max_records = max_records
        self._ttl = timedelta(hours=ttl_hours)
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()

    def _evict_expired_and_overflow_locked(self) -> None:
        now = utc_now()
        expired_ids = [
            task_id for task_id, record in self._tasks.items() if record.is_final and now - self._retention_time(record) > self._ttl
        ]
        for task_id in expired_ids:
            self._tasks.pop(task_id, None)

        overflow = len(self._tasks) - self._max_records
        if overflow <= 0:
            return

        final_records = nsmallest(
            overflow,
            (record for record in self._tasks.values() if record.is_final),
            key=self._retention_time,
        )
        for record in final_records:
            self._tasks.pop(record.task_id, None)

    def _retention_time(self, record: TaskRecord) -> datetime:
        return record.ended_at or record.created_at

    async def add(self, record: TaskRecord) -> None:
        async with self._lock:
            self._tasks[record.task_id] = record
            self._evict_expired_and_overflow_locked()

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            self._evict_expired_and_overflow_locked()
            return self._tasks.get(task_id)

    async def save(self, record: TaskRecord) -> None:
        async with self._lock:
            self._tasks[record.task_id] = record
            self._evict_expired_and_overflow_locked()

    async def list_by_user(self, user_id: int, limit: int = 10) -> list[TaskRecord]:
        async with self._lock:
            self._evict_expired_and_overflow_locked()
            items = [x for x in self._tasks.values() if x.user_id == user_id]
        items.sort(key=lambda x: x.created_at, reverse=True)
        return items[:limit]

    async def iter_all(self) -> Iterable[TaskRecord]:
        async with self._lock:
            self._evict_expired_and_overflow_locked()
            return list(self._tasks.values())


class SessionContextStore(Protocol):
    async def get(self, user_id: int) -> SessionContext | None: ...

    async def list_all(self) -> list[SessionContext]: ...

    async def save(self, session: SessionContext) -> None: ...

    async def delete(self, user_id: int) -> bool: ...

    async def get_by_claude_session_id(self, claude_session_id: str) -> SessionContext | None: ...


class MemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[int, SessionContext] = {}
        self._lock = asyncio.Lock()

    async def get(self, user_id: int) -> SessionContext | None:
        async with self._lock:
            return self._sessions.get(user_id)

    async def list_all(self) -> list[SessionContext]:
        async with self._lock:
            return list(self._sessions.values())

    async def get_by_claude_session_id(self, claude_session_id: str) -> SessionContext | None:
        async with self._lock:
            for session in self._sessions.values():
                if session.claude_session_id == claude_session_id:
                    return session
            return None

    async def save(self, session: SessionContext) -> None:
        async with self._lock:
            self._sessions[session.user_id] = session

    async def delete(self, user_id: int) -> bool:
        async with self._lock:
            if user_id in self._sessions:
                del self._sessions[user_id]
                return True
            return False
