from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from app.adapters.process.tmux_runner import TmuxRunner
from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.models import SessionContext, TerminalSessionInfo
from app.services.session_service import SessionService
from app.services.session_store import SessionStore

logger = logging.getLogger(__name__)


class SessionRegistryService:
    """Manages tmux session discovery, cross-user attach, and health checking."""

    def __init__(
        self,
        *,
        session_service: SessionService,
        session_store: SessionStore,
        tmux_runner: TmuxRunner,
        file_session_store: FileSessionStore,
        health_check_interval_sec: float = 30.0,
    ) -> None:
        self._session_service = session_service
        self._session_store = session_store
        self._tmux_runner = tmux_runner
        self._file_session_store = file_session_store
        self._health_check_interval_sec = health_check_interval_sec
        self._health_check_task: asyncio.Task[None] | None = None

    # ── Discovery ──────────────────────────────────────────────────────────────

    async def list_active_sessions(self) -> list[TerminalSessionInfo]:
        """List all tgcli_* tmux sessions that are alive."""
        tmux_names = await self._tmux_runner._list_managed_sessions()
        if not tmux_names:
            return []

        # Build lookup: terminal_id -> owner SessionContext
        all_contexts = await self._session_service.list_all()
        owner_by_terminal: dict[str, SessionContext] = {}
        attached_by_terminal: dict[str, list[int]] = {}
        for ctx in all_contexts:
            if ctx.terminal_id:
                if ctx.is_owner:
                    owner_by_terminal[ctx.terminal_id] = ctx
                else:
                    attached_by_terminal.setdefault(ctx.terminal_id, []).append(ctx.user_id)

        results: list[TerminalSessionInfo] = []
        for tmux_name in tmux_names:
            # Extract terminal_id from tmux session name: "tgcli_" + sanitized
            terminal_id = tmux_name.removeprefix("tgcli_")
            if not terminal_id:
                continue

            alive = await self._tmux_runner._session_exists(tmux_name)

            # Find SessionState for phase/workdir
            state = self._session_store.find_by_terminal_id(terminal_id)
            workdir = state.workdir if state else "unknown"
            phase = state.phase.value if state else "unknown"

            owner = owner_by_terminal.get(terminal_id)
            attached = attached_by_terminal.get(terminal_id, [])
            # Also include owner's attached_user_ids
            if owner and owner.attached_user_ids:
                attached = list(set(attached) | set(owner.attached_user_ids))

            results.append(
                TerminalSessionInfo(
                    terminal_id=terminal_id,
                    tmux_session_name=tmux_name,
                    workdir=workdir,
                    phase=phase,
                    owner_user_id=owner.user_id if owner else None,
                    attached_user_ids=attached,
                    is_alive=alive,
                )
            )

        return results

    async def get_session_info(self, terminal_id: str) -> TerminalSessionInfo | None:
        """Get info about a specific session."""
        tmux_name = self._tmux_runner._build_session_name(terminal_id)
        alive = await self._tmux_runner._session_exists(tmux_name)
        if not alive:
            return None

        state = self._session_store.find_by_terminal_id(terminal_id)
        workdir = state.workdir if state else "unknown"
        phase = state.phase.value if state else "unknown"

        all_contexts = await self._session_service.list_all()
        owner: SessionContext | None = None
        attached_ids: list[int] = []
        for ctx in all_contexts:
            if ctx.terminal_id == terminal_id:
                if ctx.is_owner:
                    owner = ctx
                else:
                    attached_ids.append(ctx.user_id)
        if owner and owner.attached_user_ids:
            attached_ids = list(set(attached_ids) | set(owner.attached_user_ids))

        return TerminalSessionInfo(
            terminal_id=terminal_id,
            tmux_session_name=tmux_name,
            workdir=workdir,
            phase=phase,
            owner_user_id=owner.user_id if owner else None,
            attached_user_ids=attached_ids,
            is_alive=alive,
        )

    # ── Attach / Detach ────────────────────────────────────────────────────────

    async def attach_user(self, *, user_id: int, terminal_id: str) -> tuple[bool, str]:
        """Attach a user to an existing session (may be another user's session)."""
        tmux_name = self._tmux_runner._build_session_name(terminal_id)
        alive = await self._tmux_runner._session_exists(tmux_name)
        if not alive:
            return False, f"会话 {terminal_id} 不存在或已关闭"

        # Check if user is already attached to this session
        current = await self._session_service.get(user_id)
        if current and current.terminal_id == terminal_id and current.claude_chat_active:
            return True, f"已连接到会话 {terminal_id}"

        # Detach from previous session if attached to a different one
        if current and current.terminal_id and current.terminal_id != terminal_id:
            await self._detach_user_internal(user_id, current)

        # Find the owner of the target session
        all_contexts = await self._session_service.list_all()
        owner: SessionContext | None = None
        for ctx in all_contexts:
            if ctx.terminal_id == terminal_id and ctx.is_owner:
                owner = ctx
                break

        # Get workdir from SessionState
        state = self._session_store.find_by_terminal_id(terminal_id)
        workdir = state.workdir if state else (owner.workdir if owner else ".")

        # Update user's SessionContext
        await self._session_service.switch(
            user_id=user_id,
            provider="claude_code",
            workdir=workdir,
            terminal_mode=True,
            claude_chat_active=True,
        )
        # Override terminal_id to point to the target session (switch() builds from user_id+workdir)
        updated = await self._session_service.get(user_id)
        if updated and updated.terminal_id != terminal_id:
            updated.terminal_id = terminal_id
            updated.is_owner = False
            # We need to save through the store directly since SessionService builds terminal_id deterministically
            await self._session_service.save_session_context(updated)

        # Add user to owner's attached_user_ids
        if owner and user_id not in owner.attached_user_ids:
            owner.attached_user_ids.append(user_id)
            await self._session_service.save_session_context(owner)

        logger.info(
            "user attached to session",
            extra={"user_id": user_id, "terminal_id": terminal_id, "owner": owner.user_id if owner else None},
        )
        return True, f"已连接到会话 {terminal_id}"

    async def detach_user(self, *, user_id: int) -> tuple[bool, str]:
        """Detach the user from their currently attached session."""
        current = await self._session_service.get(user_id)
        if not current or not current.terminal_id:
            return False, "当前未连接到任何会话"

        terminal_id = current.terminal_id
        await self._detach_user_internal(user_id, current)
        logger.info("user detached from session", extra={"user_id": user_id, "terminal_id": terminal_id})
        return True, f"已断开会话 {terminal_id}"

    async def _detach_user_internal(self, user_id: int, current: SessionContext) -> None:
        """Internal detach logic."""
        terminal_id = current.terminal_id

        # Remove from owner's attached_user_ids
        if not current.is_owner and terminal_id:
            all_contexts = await self._session_service.list_all()
            for ctx in all_contexts:
                if ctx.terminal_id == terminal_id and ctx.is_owner and user_id in ctx.attached_user_ids:
                    ctx.attached_user_ids.remove(user_id)
                    await self._session_service.save_session_context(ctx)
                    break

        # Reset user's session
        await self._session_service.switch(
            user_id=user_id,
            terminal_mode=False,
            claude_chat_active=False,
        )

    # ── Auto-reattach ──────────────────────────────────────────────────────────

    async def validate_or_reattach(self, user_id: int) -> SessionContext | None:
        """Validate that the user's session binding is alive.

        If the tmux session is dead, try to find a live session with the same terminal_id.
        Returns the (possibly updated) SessionContext, or None if no live session found.
        """
        current = await self._session_service.get(user_id)
        if not current or not current.terminal_id:
            return None

        terminal_id = current.terminal_id
        tmux_name = self._tmux_runner._build_session_name(terminal_id)
        alive = await self._tmux_runner._session_exists(tmux_name)

        if alive:
            return current

        # Tmux session is dead. Try to find a live SessionState with the same terminal_id.
        logger.info(
            "tmux session dead, attempting reattach",
            extra={"user_id": user_id, "terminal_id": terminal_id},
        )

        # Search persisted SessionState records
        all_states = self._file_session_store.list_session_states()
        for state in all_states:
            if state.terminal_id == terminal_id:
                state_tmux = self._tmux_runner._build_session_name(state.terminal_id)
                if await self._tmux_runner._session_exists(state_tmux):
                    # Found a live session with matching terminal_id
                    logger.info(
                        "reattach: found live session",
                        extra={"user_id": user_id, "terminal_id": terminal_id, "session_id": state.session_id},
                    )
                    if current.claude_session_id != state.claude_session_id:
                        await self._session_service.bind_claude_session(
                            user_id=user_id,
                            claude_session_id=state.claude_session_id or state.session_id,
                            workdir=state.workdir,
                        )
                    return await self._session_service.get(user_id)

        logger.info("reattach: no live session found", extra={"user_id": user_id, "terminal_id": terminal_id})
        return None

    # ── Health check ───────────────────────────────────────────────────────────

    async def start_health_check(self) -> None:
        """Start the periodic health check background task."""
        if self._health_check_task is not None and not self._health_check_task.done():
            return
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info("session health check started", extra={"interval_sec": self._health_check_interval_sec})

    async def stop_health_check(self) -> None:
        """Stop the health check background task."""
        task = self._health_check_task
        self._health_check_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _health_check_loop(self) -> None:
        """Periodically check session liveness."""
        while True:
            await asyncio.sleep(self._health_check_interval_sec)
            try:
                await self._run_health_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("session health check error")

    async def _run_health_check(self) -> None:
        """Scan all SessionContext records, clean up stale bindings."""
        all_contexts = await self._session_service.list_all()
        stale: list[SessionContext] = []

        for ctx in all_contexts:
            if not ctx.terminal_id:
                continue
            tmux_name = self._tmux_runner._build_session_name(ctx.terminal_id)
            alive = await self._tmux_runner._session_exists(tmux_name)
            if not alive:
                stale.append(ctx)

        if not stale:
            return

        for ctx in stale:
            logger.info(
                "health check: cleaning stale binding",
                extra={"user_id": ctx.user_id, "terminal_id": ctx.terminal_id},
            )
            ctx.claude_session_id = None
            if not ctx.is_owner:
                # Non-owner: fully detach
                ctx.terminal_mode = False
                ctx.claude_chat_active = False
                ctx.terminal_id = None
            else:
                # Owner: keep terminal_id for potential recreation, but clear session
                pass
            await self._session_service.save_session_context(ctx)
