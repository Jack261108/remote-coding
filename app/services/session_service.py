from __future__ import annotations

import uuid

from app.adapters.storage.memory import MemorySessionStore
from app.domain.models import SessionContext, utc_now


class SessionService:
    def __init__(self, store: MemorySessionStore) -> None:
        self._store = store

    async def get_or_create(
        self,
        *,
        user_id: int,
        provider: str,
        workdir: str,
        terminal_mode: bool = False,
        claude_chat_active: bool | None = None,
    ) -> SessionContext:
        current = await self._store.get(user_id)
        if current is not None:
            needs_update = (
                current.provider != provider
                or current.workdir != workdir
                or current.terminal_mode != terminal_mode
                or (terminal_mode and not current.terminal_id)
                or (claude_chat_active is not None and current.claude_chat_active != claude_chat_active)
            )
            if needs_update:
                current.provider = provider
                current.workdir = workdir
                current.terminal_mode = terminal_mode
                if terminal_mode:
                    current.terminal_id = current.terminal_id or f"user_{user_id}"
                else:
                    current.terminal_id = None
                if claude_chat_active is not None:
                    current.claude_chat_active = claude_chat_active
                current.updated_at = utc_now()
                await self._store.save(current)
            return current

        session = SessionContext(
            user_id=user_id,
            session_id=str(uuid.uuid4()),
            provider=provider,
            workdir=workdir,
            terminal_mode=terminal_mode,
            terminal_id=f"user_{user_id}" if terminal_mode else None,
            claude_chat_active=claude_chat_active or False,
        )
        await self._store.save(session)
        return session

    async def switch(
        self,
        *,
        user_id: int,
        provider: str | None = None,
        workdir: str | None = None,
        terminal_mode: bool | None = None,
        claude_chat_active: bool | None = None,
    ) -> SessionContext:
        current = await self._store.get(user_id)
        if current is None:
            tm = bool(terminal_mode)
            session = SessionContext(
                user_id=user_id,
                session_id=str(uuid.uuid4()),
                provider=provider or "claude_code",
                workdir=workdir or ".",
                terminal_mode=tm,
                terminal_id=f"user_{user_id}" if tm else None,
                claude_chat_active=claude_chat_active or False,
            )
            await self._store.save(session)
            return session

        if provider is not None:
            current.provider = provider
        if workdir is not None:
            current.workdir = workdir
        if terminal_mode is not None:
            current.terminal_mode = terminal_mode
            current.terminal_id = current.terminal_id or f"user_{user_id}" if terminal_mode else None
        if claude_chat_active is not None:
            current.claude_chat_active = claude_chat_active
        current.updated_at = utc_now()

        await self._store.save(current)
        return current

    async def get(self, user_id: int) -> SessionContext | None:
        return await self._store.get(user_id)
