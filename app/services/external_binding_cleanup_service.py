from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from app.domain.models import utc_now
from app.services.external_binding_reaper import ExternalBindingReaper
from app.services.external_binding_store import ExternalBindingStore
from app.services.process_liveness import process_is_alive

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupDecision:
    action: Literal["keep", "remove"]
    reason: str | None  # "pid_dead" | "idle_ttl_expired" | None


def decide_cleanup(
    *,
    liveness_enabled: bool,
    pid_known: bool,
    pid_alive: bool,  # only consulted when pid_known
    idle_expired: bool,  # idle_age > ttl
    has_pending_permission: bool,
) -> CleanupDecision:
    # Rows 1-3: liveness governs when enabled AND pid known.
    if liveness_enabled and pid_known:
        if pid_alive:
            return CleanupDecision("keep", None)  # row 1
        return CleanupDecision("remove", "pid_dead")  # rows 2, 3
    # Rows 4-9: idle-TTL fallback (pid unknown OR liveness disabled).
    if idle_expired and not has_pending_permission:
        return CleanupDecision("remove", "idle_ttl_expired")  # rows 4, 7
    return CleanupDecision("keep", None)  # rows 5, 6, 8, 9


class ExternalBindingCleanupService:
    """Periodically remove external bindings whose owning process is dead or
    whose idle age exceeds the TTL.

    The cleanup decision is governed by the normative Decision Matrix in
    ``requirements.md``:

    - When pid liveness is enabled AND the binding's pid is known
      (``pid is not None and pid > 0``), liveness governs the decision: a
      live pid retains the binding regardless of idle age, and a dead pid
      removes the binding regardless of idle age and pending-permission
      state (Decision Matrix rows 1-3).
    - Otherwise (pid unknown OR liveness disabled), the binding falls back
      to the existing idle-TTL path with race-safe re-reads and the
      pending-permission protection signal (rows 4-9).

    Architectural note: this service intentionally does NOT depend on
    ``PermissionCallbackRegistry``. Idle TTL is a heuristic, not proof of
    death, so we deliberately avoid invalidating callback registry entries
    here (per spec T12c). The dead-process path delegates the canonical
    removal-and-cleanup sequence to the shared ``ExternalBindingReaper``,
    which is also used by ``/list`` so the order lives in exactly one
    place.
    """

    def __init__(
        self,
        *,
        binding_store: ExternalBindingStore,
        hook_socket_server: HookSocketServer,
        reaper: ExternalBindingReaper,
        liveness_enabled: bool,
        ttl: timedelta,
        interval_sec: float,
    ) -> None:
        self._binding_store = binding_store
        self._hook_socket_server = hook_socket_server
        self._reaper = reaper
        self._liveness_enabled = liveness_enabled
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
        """Apply the Decision Matrix to every binding with race-safe re-reads.

        For each binding: when liveness is enabled AND the pid is known the
        liveness probe governs (rows 1-3); otherwise the existing idle-TTL
        path runs verbatim with its race-safe re-reads and pending-permission
        protection signal (rows 4-9). The actual removal-and-cleanup sequence
        is delegated to the shared reaper so its canonical order lives in
        exactly one place.
        """
        now = utc_now()
        snapshot = self._binding_store.list_all()

        for binding in snapshot:
            session_id = binding.session_id
            pid = binding.pid

            # Rows 1-3: liveness governs when enabled AND pid known. A live
            # pid retains the binding regardless of idle age (Req 5.1, 5.2);
            # a dead pid removes it regardless of idle age and pending
            # permission (Req 6.1-6.3). The reaper performs its own re-read
            # guard before removal (Req 6.6), so no explicit re-read here.
            if self._liveness_enabled and pid is not None and pid > 0:
                if process_is_alive(pid):
                    continue
                await self._reaper.remove_with_cleanup(session_id, reason="pid_dead")
                continue

            # Rows 4-9: idle-TTL fallback (pid unknown OR liveness disabled).

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
            # Re-fetch now to get accurate idle age after await.
            now = utc_now()
            current = self._binding_store.get_binding(session_id)
            if current is None:
                continue
            idle_age = now - current.last_activity_at
            if idle_age <= self._ttl:
                continue

            # Step vi: delegate the canonical removal sequence to the reaper.
            # The reaper drops the binding, clears auto-approve state, cancels
            # pending permissions, and emits the INFO log itself.
            await self._reaper.remove_with_cleanup(session_id, reason="idle_ttl_expired")
