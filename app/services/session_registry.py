from __future__ import annotations

import asyncio
import logging

from app.adapters.process.tmux_runner import TmuxRunner
from app.domain.models import SessionContext, TerminalSessionInfo, utc_now
from app.infra.async_utils import cancel_optional_task
from app.services.auto_approve_service import AutoApproveService
from app.services.session_lookup_service import SessionLookupService
from app.services.session_service import SessionService
from app.services.session_state_repository import SessionStateRepository

logger = logging.getLogger(__name__)


class SessionRegistryService:
    """Manages tmux session discovery, cross-user attach, and health checking."""

    def __init__(
        self,
        *,
        session_service: SessionService,
        lookup: SessionLookupService,
        tmux_runner: TmuxRunner,
        repository: SessionStateRepository,
        auto_approve_service: AutoApproveService | None = None,
        health_check_interval_sec: float = 30.0,
    ) -> None:
        self._session_service = session_service
        self._lookup = lookup
        self._tmux_runner = tmux_runner
        self._repository = repository
        self._auto_approve_service = auto_approve_service
        self._health_check_interval_sec = health_check_interval_sec
        self._health_check_task: asyncio.Task[None] | None = None

    # ── Helpers ─────────────────────────────────────────────────────────────────

    async def _find_owner_by_terminal(self, terminal_id: str) -> SessionContext | None:
        """Find the owner SessionContext for a given terminal_id."""
        all_contexts = await self._session_service.list_all()
        for ctx in all_contexts:
            if ctx.terminal_id == terminal_id and ctx.is_owner:
                return ctx
        return None

    async def _classify_users_by_terminal(self, terminal_id: str) -> tuple[SessionContext | None, list[int]]:
        """Return (owner, attached_user_ids) for a given terminal_id.

        ``attached_user_ids`` merges non-owner context user_ids with the
        owner's own ``attached_user_ids`` list (deduplicated).
        """
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
        return owner, attached_ids

    def _terminal_lock(self, terminal_id: str):
        return self._session_service.terminal_group_lock(terminal_id)

    async def _check_session_liveness(self, tmux_name: str, **log_extra: object) -> bool | None:
        """Check if a tmux session is alive. Returns None if unable to determine."""
        try:
            return await self._tmux_runner.session_exists(tmux_name)
        except Exception:
            logger.warning("cannot determine session liveness", extra={"tmux_name": tmux_name, **log_extra})
            return None

    # ── Discovery ──────────────────────────────────────────────────────────────

    async def list_active_sessions(self) -> list[TerminalSessionInfo]:
        """List all tgcli_* tmux sessions that are alive."""
        tmux_names = await self._tmux_runner.list_managed_sessions()
        if not tmux_names:
            return []

        results: list[TerminalSessionInfo] = []
        for tmux_name in tmux_names:
            # Extract terminal_id from tmux session name: "tgcli_" + sanitized
            terminal_id = tmux_name.removeprefix("tgcli_")
            if not terminal_id:
                continue

            alive = await self._check_session_liveness(tmux_name)
            if alive is None:
                alive = False

            # Find SessionState for phase/workdir
            state = self._lookup.find_by_terminal_id(terminal_id)
            workdir = state.workdir if state else "unknown"
            phase = state.phase.value if state else "unknown"

            owner, attached = await self._classify_users_by_terminal(terminal_id)

            results.append(
                TerminalSessionInfo(
                    terminal_id=terminal_id,
                    tmux_session_name=tmux_name,
                    workdir=workdir,
                    phase=phase,
                    owner_user_id=owner.user_id if owner else None,
                    attached_user_ids=attached,
                    is_alive=alive,
                    last_activity=state.last_activity if state else None,
                )
            )

        return results

    async def get_session_info(self, terminal_id: str) -> TerminalSessionInfo | None:
        """Get info about a specific session."""
        tmux_name = self._tmux_runner.build_session_name(terminal_id)
        alive = await self._check_session_liveness(tmux_name, terminal_id=terminal_id)
        if alive is None:
            return None
        if not alive:
            return None

        state = self._lookup.find_by_terminal_id(terminal_id)
        workdir = state.workdir if state else "unknown"
        phase = state.phase.value if state else "unknown"

        owner, attached_ids = await self._classify_users_by_terminal(terminal_id)

        return TerminalSessionInfo(
            terminal_id=terminal_id,
            tmux_session_name=tmux_name,
            workdir=workdir,
            phase=phase,
            owner_user_id=owner.user_id if owner else None,
            attached_user_ids=attached_ids,
            is_alive=alive,
            last_activity=state.last_activity if state else None,
        )

    # ── Attach / Detach ────────────────────────────────────────────────────────

    async def attach_user(self, *, user_id: int, terminal_id: str) -> tuple[bool, str]:
        """Attach a user to an existing session (may be another user's session)."""
        tmux_name = self._tmux_runner.build_session_name(terminal_id)
        alive = await self._check_session_liveness(tmux_name, terminal_id=terminal_id, user_id=user_id)
        if alive is None:
            return False, f"无法判断会话 {terminal_id} 状态，请稍后重试"
        if not alive:
            return False, f"会话 {terminal_id} 不存在或已关闭"

        current = await self._session_service.get(user_id)
        previous_terminal_id = current.terminal_id if current and current.terminal_id != terminal_id else None
        if previous_terminal_id is not None:
            async with self._terminal_lock(previous_terminal_id):
                current = await self._session_service.get(user_id)
                if current and current.terminal_id == previous_terminal_id:
                    await self._detach_user_internal(user_id, current)

        async with self._terminal_lock(terminal_id):
            alive = await self._check_session_liveness(tmux_name, terminal_id=terminal_id, user_id=user_id)
            if alive is None:
                return False, f"无法判断会话 {terminal_id} 状态，请稍后重试"
            if not alive:
                return False, f"会话 {terminal_id} 不存在或已关闭"

            # Check if user is already attached to this session
            current = await self._session_service.get(user_id)
            if current and current.terminal_id == terminal_id and current.claude_chat_active:
                return True, f"已连接到会话 {terminal_id}"
            if current and current.terminal_id and current.terminal_id != terminal_id:
                return False, "当前会话状态已变化，请重试"

            # Find the owner of the target session
            owner = await self._find_owner_by_terminal(terminal_id)

            # Get workdir from SessionState
            state = self._lookup.find_by_terminal_id(terminal_id)
            workdir = state.workdir if state else (owner.workdir if owner else ".")

            # Update user's SessionContext
            _, orphaned = await self._session_service.switch(
                user_id=user_id,
                provider="claude_code",
                workdir=workdir,
                terminal_mode=True,
                claude_chat_active=True,
            )
            # Clean up orphaned terminal resources if detected
            if orphaned is not None:
                logger.info(
                    "cleaning up orphaned terminal during attach",
                    extra={
                        "terminal_id": orphaned.terminal_id,
                        "claude_session_id": orphaned.claude_session_id,
                        "user_id": orphaned.user_id,
                    },
                )
                if self._auto_approve_service is not None and orphaned.claude_session_id:
                    await self._auto_approve_service.clear_session(orphaned.claude_session_id)
                await self._session_service.clear_terminal_group(orphaned.terminal_id)
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
        async with self._terminal_lock(terminal_id):
            current = await self._session_service.get(user_id)
            if not current or not current.terminal_id:
                return False, "当前未连接到任何会话"
            if current.terminal_id != terminal_id:
                return False, "当前会话状态已变化，请重试"
            await self._detach_user_internal(user_id, current)
        logger.info("user detached from session", extra={"user_id": user_id, "terminal_id": terminal_id})
        return True, f"已断开会话 {terminal_id}"

    async def close_session(self, terminal_id: str) -> bool:
        """Kill a tmux session and clean up associated state."""
        async with self._terminal_lock(terminal_id):
            close_result = await self._tmux_runner.close_terminal(terminal_id)
            ok = close_result[0] if isinstance(close_result, tuple) else close_result
            if ok:
                contexts = [ctx for ctx in await self._session_service.list_all() if ctx.terminal_id == terminal_id]
                claude_session_ids = sorted({ctx.claude_session_id for ctx in contexts if ctx.claude_session_id})
                if self._auto_approve_service is not None:
                    for session_id in claude_session_ids:
                        await self._auto_approve_service.clear_session(session_id)
                await self._session_service.clear_terminal_group(terminal_id)
                logger.info("session closed", extra={"terminal_id": terminal_id})
            return ok

    async def _detach_user_internal(self, user_id: int, current: SessionContext) -> None:
        """Internal detach logic."""
        terminal_id = current.terminal_id

        # Remove from owner's attached_user_ids
        if not current.is_owner and terminal_id:
            owner = await self._find_owner_by_terminal(terminal_id)
            if owner and user_id in owner.attached_user_ids:
                owner.attached_user_ids.remove(user_id)
                await self._session_service.save_session_context(owner)

        # Reset user's session
        _, orphaned = await self._session_service.switch(
            user_id=user_id,
            terminal_mode=False,
            claude_chat_active=False,
        )
        # Clean up orphaned terminal resources if detected
        if orphaned is not None:
            logger.info(
                "cleaning up orphaned terminal during detach",
                extra={
                    "terminal_id": orphaned.terminal_id,
                    "claude_session_id": orphaned.claude_session_id,
                    "user_id": orphaned.user_id,
                },
            )
            if self._auto_approve_service is not None and orphaned.claude_session_id:
                await self._auto_approve_service.clear_session(orphaned.claude_session_id)
            await self._session_service.clear_terminal_group(orphaned.terminal_id)

    # ── Auto-reattach ──────────────────────────────────────────────────────────

    async def validate_or_reattach(self, user_id: int) -> SessionContext | None:
        """Validate that the user's session binding is alive.

        If the tmux session is dead, try to find another live session for the same user/workdir.
        Returns the (possibly updated) SessionContext, or None if no live session found.
        """
        current = await self._session_service.get(user_id)
        if not current or not current.terminal_id:
            return None

        terminal_id = current.terminal_id
        tmux_name = self._tmux_runner.build_session_name(terminal_id)
        alive = await self._check_session_liveness(tmux_name, user_id=user_id, terminal_id=terminal_id)
        if alive is None:
            return current

        if alive:
            return current

        # Tmux session is dead. Try to find another live SessionState for this user/workdir.
        logger.info(
            "tmux session dead, attempting reattach",
            extra={"user_id": user_id, "terminal_id": terminal_id},
        )

        live_states = []
        for state in self._repository.list_states():
            if (
                state.user_id != user_id
                or state.provider != current.provider
                or state.workdir != current.workdir
                or not state.terminal_id
                or state.terminal_id == terminal_id
            ):
                continue
            state_tmux = self._tmux_runner.build_session_name(state.terminal_id)
            alive = await self._check_session_liveness(state_tmux, terminal_id=state.terminal_id)
            if alive is None:
                continue
            if alive:
                live_states.append(state)

        if live_states:
            state = max(live_states, key=lambda candidate: (candidate.last_activity, candidate.created_at, candidate.revision))
            logger.info(
                "reattach: found live session",
                extra={"user_id": user_id, "terminal_id": state.terminal_id, "session_id": state.session_id},
            )
            current.terminal_id = state.terminal_id
            current.claude_session_id = state.claude_session_id or state.session_id
            current.workdir = state.workdir
            current.updated_at = utc_now()
            await self._session_service.save_session_context(current)
            return await self._session_service.get(user_id)

        logger.info("reattach: no live session found", extra={"user_id": user_id, "terminal_id": terminal_id})
        return None

    # ── Health check ───────────────────────────────────────────────────────────

    async def reconcile_terminal_lifecycle(self) -> None:
        """Run one tmux terminal lifecycle reconciliation pass."""
        await self._run_health_check()

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
        await cancel_optional_task(task)

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

        # Filter contexts with terminal bindings and check liveness
        contexts_with_terminals = [ctx for ctx in all_contexts if ctx.terminal_id]
        if not contexts_with_terminals:
            return

        tmux_names = [self._tmux_runner.build_session_name(ctx.terminal_id) for ctx in contexts_with_terminals]  # type: ignore[arg-type]

        stale: list[SessionContext] = []
        for ctx, tmux_name in zip(contexts_with_terminals, tmux_names, strict=False):
            alive = await self._check_session_liveness(tmux_name, user_id=ctx.user_id, terminal_id=ctx.terminal_id)
            if alive is None:
                continue
            if not alive:
                stale.append(ctx)

        if not stale:
            return

        stale_terminal_ids = sorted({ctx.terminal_id for ctx in stale if ctx.terminal_id})

        for terminal_id in stale_terminal_ids:
            async with self._terminal_lock(terminal_id):
                tmux_name = self._tmux_runner.build_session_name(terminal_id)
                alive = await self._check_session_liveness(tmux_name, terminal_id=terminal_id)
                if alive is None or alive:
                    continue

                contexts = [ctx for ctx in await self._session_service.list_all() if ctx.terminal_id == terminal_id]
                claude_session_ids = sorted({ctx.claude_session_id for ctx in contexts if ctx.claude_session_id})
                if self._auto_approve_service is not None:
                    for session_id in claude_session_ids:
                        await self._auto_approve_service.clear_session(session_id)

                logger.info(
                    "health check: cleaning stale binding",
                    extra={"terminal_id": terminal_id},
                )
                await self._session_service.clear_terminal_group(terminal_id)
