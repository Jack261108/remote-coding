from __future__ import annotations

import asyncio

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.models import SessionContext


class FileSessionContextStore:
    def __init__(self, file_store: FileSessionStore) -> None:
        self._file_store = file_store
        self._lock = asyncio.Lock()
        self._list_cache: list[SessionContext] | None = None
        self._claude_session_index: dict[str, SessionContext] = {}

    async def get(self, user_id: int) -> SessionContext | None:
        async with self._lock:
            return self._file_store.load_session_context(user_id)

    async def list_all(self) -> list[SessionContext]:
        async with self._lock:
            if self._list_cache is not None:
                return list(self._list_cache)
            contexts = self._file_store.list_session_contexts()
            self._list_cache = contexts
            # Rebuild index from loaded contexts
            self._claude_session_index = {ctx.claude_session_id: ctx for ctx in contexts if ctx.claude_session_id}
            return list(contexts)

    async def get_by_claude_session_id(self, claude_session_id: str) -> SessionContext | None:
        """O(1) index lookup by claude_session_id. Returns None on miss."""
        async with self._lock:
            return self._claude_session_index.get(claude_session_id)

    async def save(self, session: SessionContext) -> None:
        async with self._lock:
            # Remove stale index entry if claude_session_id changed or was cleared
            old = self._file_store.load_session_context(session.user_id)
            if old is not None and old.claude_session_id and old.claude_session_id != session.claude_session_id:
                self._claude_session_index.pop(old.claude_session_id, None)

            # Persist
            self._file_store.save_session_context(session)
            self._list_cache = None  # invalidate cache on write

            # Update index (last-writer-wins when two contexts share the same id)
            if session.claude_session_id:
                self._claude_session_index[session.claude_session_id] = session
