from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.adapters.storage.memory import SessionContextStore
from app.domain.models import SessionContext, utc_now
from app.infra.lock_registry import RefCountedLockRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrphanedTerminalInfo:
    """Information about a terminal that was orphaned during session update."""

    terminal_id: str
    claude_session_id: str | None
    user_id: int


class UserSessionContextService:
    """Manages user-level session context (provider, workdir, terminal_mode)."""

    def __init__(self, store: SessionContextStore) -> None:
        self._store = store
        self._terminal_locks = RefCountedLockRegistry(
            ttl_sec=300,  # 5 minutes
            cleanup_interval_sec=60,  # 1 minute
            cleanup_batch_size=10,
        )

    @asynccontextmanager
    async def terminal_group_lock(self, terminal_id: str) -> AsyncIterator[None]:
        """Acquire a lock for a terminal group.

        The lock is automatically released when the context manager exits.
        Unused locks are cleaned up after TTL expires.
        """
        async with self._terminal_locks.lock(terminal_id):
            yield None

    def _build_terminal_id(self, *, user_id: int, workdir: str) -> str:
        digest = hashlib.sha1(workdir.encode("utf-8")).hexdigest()[:12]
        return f"user_{user_id}_{digest}"

    async def _update_or_create_session(
        self,
        *,
        user_id: int,
        provider: str,
        workdir: str,
        terminal_mode: bool,
        claude_chat_active: bool | None = None,
        existing: SessionContext | None = None,
        previous_workdir: str | None = None,
        previous_provider: str | None = None,
        rebuild_terminal_id: bool = True,
    ) -> tuple[SessionContext, OrphanedTerminalInfo | None]:
        """Shared logic for creating or updating a session context.

        Returns:
            Tuple of (session, orphaned_terminal_info). The orphaned_terminal_info
            is set when a terminal was orphaned due to workdir/provider change,
            and the caller is responsible for cleaning up the old terminal resources.
        """
        if existing is None:
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
            return session, None

        # Capture old terminal info before modification for orphan detection
        old_terminal_id = existing.terminal_id
        old_claude_session_id = existing.claude_session_id

        existing.provider = provider
        existing.workdir = workdir
        existing.terminal_mode = terminal_mode

        if rebuild_terminal_id:
            existing.terminal_id = self._build_terminal_id(user_id=user_id, workdir=workdir) if terminal_mode else None

        if claude_chat_active is not None:
            existing.claude_chat_active = claude_chat_active

        workdir_changed = previous_workdir is not None and workdir != previous_workdir
        provider_changed = previous_provider is not None and provider != previous_provider

        orphaned: OrphanedTerminalInfo | None = None
        if provider != "claude_code" or workdir_changed or provider_changed:
            existing.claude_session_id = None
            # Detect orphaned terminal: old terminal_id exists and either
            # workdir/provider changed or terminal_id will be rebuilt
            if old_terminal_id is not None and (workdir_changed or provider_changed):
                orphaned = OrphanedTerminalInfo(
                    terminal_id=old_terminal_id,
                    claude_session_id=old_claude_session_id,
                    user_id=user_id,
                )
                logger.info(
                    "detected orphaned terminal during session update",
                    extra={
                        "user_id": user_id,
                        "old_terminal_id": old_terminal_id,
                        "old_claude_session_id": old_claude_session_id,
                        "workdir_changed": workdir_changed,
                        "provider_changed": provider_changed,
                    },
                )

        existing.updated_at = utc_now()
        await self._store.save(existing)
        return existing, orphaned

    async def get_or_create(
        self,
        *,
        user_id: int,
        provider: str,
        workdir: str,
        terminal_mode: bool = False,
        claude_chat_active: bool | None = None,
    ) -> tuple[SessionContext, OrphanedTerminalInfo | None]:
        """Get or create a session context.

        Returns:
            Tuple of (session, orphaned_terminal_info). The orphaned_terminal_info
            is set when a terminal was orphaned due to workdir/provider change,
            and the caller is responsible for cleaning up the old terminal resources.
        """
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
                return await self._update_or_create_session(
                    user_id=user_id,
                    provider=provider,
                    workdir=workdir,
                    terminal_mode=terminal_mode,
                    claude_chat_active=claude_chat_active,
                    existing=current,
                    previous_workdir=current.workdir,
                    previous_provider=current.provider,
                )
            return current, None

        return await self._update_or_create_session(
            user_id=user_id,
            provider=provider,
            workdir=workdir,
            terminal_mode=terminal_mode,
            claude_chat_active=claude_chat_active,
        )

    async def switch(
        self,
        *,
        user_id: int,
        provider: str | None = None,
        workdir: str | None = None,
        terminal_mode: bool | None = None,
        claude_chat_active: bool | None = None,
    ) -> tuple[SessionContext, OrphanedTerminalInfo | None]:
        """Switch session context.

        Returns:
            Tuple of (session, orphaned_terminal_info). The orphaned_terminal_info
            is set when a terminal was orphaned due to workdir/provider change,
            and the caller is responsible for cleaning up the old terminal resources.
        """
        current = await self._store.get(user_id)
        if current is None:
            tm = bool(terminal_mode)
            return await self._update_or_create_session(
                user_id=user_id,
                provider=provider or "claude_code",
                workdir=workdir or ".",
                terminal_mode=tm,
                claude_chat_active=claude_chat_active,
            )

        previous_workdir = current.workdir
        previous_provider = current.provider

        resolved_provider = provider if provider is not None else current.provider
        resolved_workdir = workdir if workdir is not None else current.workdir
        resolved_terminal_mode = terminal_mode if terminal_mode is not None else current.terminal_mode

        rebuild_terminal_id = False
        if resolved_terminal_mode:
            if terminal_mode is not None or (workdir is not None and resolved_workdir != previous_workdir):
                rebuild_terminal_id = True
            elif not current.terminal_id:
                rebuild_terminal_id = True
        else:
            rebuild_terminal_id = True

        return await self._update_or_create_session(
            user_id=user_id,
            provider=resolved_provider,
            workdir=resolved_workdir,
            terminal_mode=resolved_terminal_mode,
            claude_chat_active=claude_chat_active,
            existing=current,
            previous_workdir=previous_workdir,
            previous_provider=previous_provider,
            rebuild_terminal_id=rebuild_terminal_id,
        )

    async def get(self, user_id: int) -> SessionContext | None:
        return await self._store.get(user_id)

    async def save_session_context(self, session: SessionContext) -> None:
        """Save a session context directly (for cross-user attach/detach)."""
        await self._store.save(session)

    async def lookup_by_claude_session_id(self, claude_session_id: str) -> SessionContext | None:
        """O(1) lookup by claude_session_id via the store's index."""
        return await self._store.get_by_claude_session_id(claude_session_id)

    async def list_all(self) -> list[SessionContext]:
        return await self._store.list_all()

    async def clear_terminal_group(self, terminal_id: str) -> list[int]:
        affected_user_ids: list[int] = []
        for ctx in await self.list_all():
            if ctx.terminal_id != terminal_id:
                continue
            ctx.terminal_mode = False
            ctx.terminal_id = None
            ctx.claude_chat_active = False
            ctx.claude_session_id = None
            ctx.attached_user_ids = []
            ctx.is_owner = True
            ctx.updated_at = utc_now()
            await self.save_session_context(ctx)
            affected_user_ids.append(ctx.user_id)
        # Clean up terminal lock to prevent memory leak
        await self._terminal_locks.cleanup_key(terminal_id, require_expired=False)
        return sorted(affected_user_ids)

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

    async def delete(self, user_id: int) -> bool:
        """Delete a session context by user_id.

        Returns:
            True if the session was deleted, False if it didn't exist.
        """
        return await self._store.delete(user_id)


# Backward-compat alias
SessionService = UserSessionContextService
