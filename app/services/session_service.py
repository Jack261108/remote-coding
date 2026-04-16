from __future__ import annotations

import hashlib
import uuid

from app.adapters.storage.memory import SessionContextStore
from app.domain.models import SessionContext, utc_now


class SessionService:
    def __init__(self, store: SessionContextStore) -> None:
        self._store = store

    def _build_terminal_id(self, *, user_id: int, workdir: str) -> str:
        digest = hashlib.sha1(workdir.encode("utf-8")).hexdigest()[:12]
        return f"user_{user_id}_{digest}"

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
                    current.terminal_id = self._build_terminal_id(user_id=user_id, workdir=workdir)
                else:
                    current.terminal_id = None
                if claude_chat_active is not None:
                    current.claude_chat_active = claude_chat_active
                if provider is not None and provider != "claude_code":
                    current.claude_session_id = None
                current.updated_at = utc_now()
                await self._store.save(current)
            return current

        session = SessionContext(
            user_id=user_id,
            session_id=str(uuid.uuid4()),
            provider=provider,
            workdir=workdir,
            terminal_mode=terminal_mode,
            terminal_id=self._build_terminal_id(user_id=user_id, workdir=workdir) if terminal_mode else None,
            claude_chat_active=claude_chat_active or False,
            claude_session_id=None,
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
            resolved_workdir = workdir or "."
            session = SessionContext(
                user_id=user_id,
                session_id=str(uuid.uuid4()),
                provider=provider or "claude_code",
                workdir=resolved_workdir,
                terminal_mode=tm,
                terminal_id=self._build_terminal_id(user_id=user_id, workdir=resolved_workdir) if tm else None,
                claude_chat_active=claude_chat_active or False,
                claude_session_id=None,
            )
            await self._store.save(session)
            return session

        if provider is not None:
            current.provider = provider
        if workdir is not None:
            current.workdir = workdir
        if terminal_mode is not None:
            current.terminal_mode = terminal_mode
            current.terminal_id = self._build_terminal_id(user_id=user_id, workdir=current.workdir) if terminal_mode else None
        if claude_chat_active is not None:
            current.claude_chat_active = claude_chat_active
        if provider is not None and provider != "claude_code":
            current.claude_session_id = None
        current.updated_at = utc_now()

        await self._store.save(current)
        return current

    async def get(self, user_id: int) -> SessionContext | None:
        return await self._store.get(user_id)

    async def list_all(self) -> list[SessionContext]:
        return await self._store.list_all()

    async def bind_claude_session(self, *, user_id: int, claude_session_id: str, workdir: str | None = None) -> SessionContext | None:
        current = await self._store.get(user_id)
        if current is None:
            return None
        current.claude_session_id = claude_session_id
        if workdir is not None:
            current.workdir = workdir
            if current.terminal_mode:
                current.terminal_id = self._build_terminal_id(user_id=user_id, workdir=workdir)
        current.updated_at = utc_now()
        await self._store.save(current)
        return current

    async def clear_claude_session(self, *, user_id: int) -> SessionContext | None:
        current = await self._store.get(user_id)
        if current is None:
            return None
        current.claude_session_id = None
        current.updated_at = utc_now()
        await self._store.save(current)
        return current
