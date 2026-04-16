from __future__ import annotations

import asyncio
from collections.abc import Iterable

from app.domain.models import SessionContext, TaskRecord


class MemoryTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()

    async def add(self, record: TaskRecord) -> None:
        async with self._lock:
            self._tasks[record.task_id] = record

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def save(self, record: TaskRecord) -> None:
        async with self._lock:
            self._tasks[record.task_id] = record

    async def list_by_user(self, user_id: int, limit: int = 10) -> list[TaskRecord]:
        async with self._lock:
            items = [x for x in self._tasks.values() if x.user_id == user_id]
        items.sort(key=lambda x: x.created_at, reverse=True)
        return items[:limit]

    async def iter_all(self) -> Iterable[TaskRecord]:
        async with self._lock:
            return list(self._tasks.values())


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

    async def save(self, session: SessionContext) -> None:
        async with self._lock:
            self._sessions[session.user_id] = session
