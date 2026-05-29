from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from typing import TYPE_CHECKING

from app.domain.models import utc_now
from app.services.auto_approve_service import AutoApproveService
from app.services.external_binding_store import ExternalBindingStore

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer

logger = logging.getLogger(__name__)


class ExternalBindingCleanupService:
    """Periodically remove external bindings whose idle age exceeds the TTL.

    Bindings with a pending permission for the corresponding session are
    protected from removal even if their idle TTL is exceeded — pending
    permissions are a strong signal that the session is still meaningfully
    "alive" from the user's perspective.

    Architectural note: this service intentionally does NOT depend on
    ``PermissionCallbackRegistry``. Idle TTL is a heuristic, not proof of
    death, so we deliberately avoid invalidating callback registry entries
    here (per spec T12c).
    """

    def __init__(
        self,
        *,
        binding_store: ExternalBindingStore,
        auto_approve_service: AutoApproveService,
        hook_socket_server: HookSocketServer,
        ttl: timedelta,
        interval_sec: float,
    ) -> None:
        self._binding_store = binding_store
        self._auto_approve_service = auto_approve_service
        self._hook_socket_server = hook_socket_server
        self._ttl = ttl
        self._interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Run an initial cleanup pass, then start the periodic task.

        Running cleanup once before the periodic loop ensures stale bindings
        from a previous process lifetime (e.g. surviving a crash) are
        eliminated before the bot starts serving requests.
        """
        await self._cleanup()
        self._task = asyncio.create_task(self._periodic_loop())

    async def stop(self) -> None:
        """Cancel the periodic task and await its termination.

        Idempotent: safe to call multiple times or before ``start()``.
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _periodic_loop(self) -> None:
        """Sleep then cleanup, recovering from per-iteration failures.

        ``asyncio.CancelledError`` propagates so ``stop()`` can terminate the
        task. All other exceptions are logged and swallowed so that one bad
        cleanup pass cannot kill the periodic task permanently.
        """
        while True:
            try:
                await asyncio.sleep(self._interval_sec)
                await self._cleanup()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("external binding cleanup iteration failed")

    async def _cleanup(self) -> None:
        """Remove every binding whose idle age exceeds the TTL and which has
        no pending permission, with race-safe re-reads around the await.
        """
        now = utc_now()
        snapshot = self._binding_store.list_all()

        for binding in snapshot:
            session_id = binding.session_id

            # Step i: snapshot pre-filter (no await) — cheap reject for fresh
            # bindings before doing any further work.
            idle_age = now - binding.last_activity_at
            if idle_age <= self._ttl:
                continue

            # Step ii: first re-read — the binding may have been removed since
            # the snapshot was taken (e.g. SessionEnd handler ran).
            current = self._binding_store.get_binding(session_id)
            if current is None:
                continue

            # Step iii: recompute idle age against the live state. A concurrent
            # ``touch_activity`` may have refreshed it between snapshot and now.
            idle_age = now - current.last_activity_at
            if idle_age <= self._ttl:
                continue

            # Step iv: pending-permission protection signal. This is the only
            # await before the final re-read, so concurrent activity can race
            # past it.
            has_pending = await self._hook_socket_server.has_pending_permission(session_id=session_id)
            if has_pending:
                continue

            # Step v: final re-read after the await closes the race window.
            current = self._binding_store.get_binding(session_id)
            if current is None:
                continue
            idle_age = now - current.last_activity_at
            if idle_age <= self._ttl:
                continue

            # Step vi: remove the binding and clean up associated session
            # state. Order matters: drop the binding first so any concurrent
            # observer sees it gone, then clear auto-approve and cancel any
            # straggling pending permissions.
            self._binding_store.remove_binding(session_id)
            await self._auto_approve_service.clear_session(session_id)
            await self._hook_socket_server.cancel_pending_permissions(session_id=session_id)

            logger.info(
                "stale external binding removed",
                extra={
                    "session_id": session_id,
                    "user_id": current.user_id,
                    "cwd": current.cwd,
                    "bound_at": current.bound_at.isoformat(),
                    "last_activity_at": current.last_activity_at.isoformat(),
                    "idle_hours": idle_age.total_seconds() / 3600,
                    "reason": "idle_ttl_expired",
                },
            )
