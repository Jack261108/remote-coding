from __future__ import annotations

import asyncio

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.models import SessionContext


class FileSessionContextStore:
    def __init__(self, file_store: FileSessionStore) -> None:
        self._file_store = file_store
        self._lock = asyncio.Lock()

    async def get(self, user_id: int) -> SessionContext | None:
        async with self._lock:
            return self._file_store.load_session_context(user_id)

    async def list_all(self) -> list[SessionContext]:
        async with self._lock:
            return self._file_store.list_session_contexts()

    async def save(self, session: SessionContext) -> None:
        async with self._lock:
            self._file_store.save_session_context(session)
