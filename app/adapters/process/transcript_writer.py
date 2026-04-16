from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.transcript_models import TranscriptEntry


class TranscriptWriter:
    def __init__(self, store: FileSessionStore) -> None:
        self._store = store
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def append_event(self, session_id: str, entry: TranscriptEntry) -> None:
        async with self._locks[session_id]:
            path = self._store.events_path(session_id)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
