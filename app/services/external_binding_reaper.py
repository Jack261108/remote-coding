"""Shared removal-and-cleanup collaborator for external bindings.

This module owns the canonical sequence used to remove an
``ExternalBinding`` and unwind its associated session state. The sequence is
invoked by BOTH the periodic cleanup loop in ``ExternalBindingCleanupService``
AND the proactive `/list` render (per Requirements 6.4 and 9.2).
Centralizing it here guarantees the order lives in exactly one place and is
identical regardless of which path observes a removable binding first.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.domain.models import utc_now
from app.domain.session_tombstone import SessionTombstoneStore
from app.services.auto_approve_service import AutoApproveService
from app.services.external_binding_store import ExternalBindingStore

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer
    from app.services.external_session_discovery import ExternalSessionDiscoveryService
    from app.services.external_user_question_state import ExternalUserQuestionState
    from app.services.permission_callback_registry import PermissionCallbackRegistry

logger = logging.getLogger(__name__)


class ExternalBindingReaper:
    """Performs the single canonical removal-and-cleanup sequence used by
    both the cleanup loop and the `/list` handler (Requirements 6.4, 9.2).
    """

    def __init__(
        self,
        *,
        binding_store: ExternalBindingStore,
        auto_approve_service: AutoApproveService,
        hook_socket_server: HookSocketServer,
        permission_callback_registry: PermissionCallbackRegistry | None = None,
        external_uq_state: ExternalUserQuestionState | None = None,
        external_discovery: ExternalSessionDiscoveryService | None = None,
        tombstone: SessionTombstoneStore | None = None,
    ) -> None:
        self._binding_store = binding_store
        self._auto_approve_service = auto_approve_service
        self._hook_socket_server = hook_socket_server
        self._permission_callback_registry = permission_callback_registry
        self._external_uq_state = external_uq_state
        self._external_discovery = external_discovery
        self._tombstone = tombstone or SessionTombstoneStore()

    async def remove_with_cleanup(self, session_id: str, *, reason: str) -> bool:
        """Atomically remove a binding and unwind its associated state.

        Steps (canonical order — do not reorder):
          1. Re-read the binding via ``get_binding``; if it is already gone
             (e.g. a concurrent ``SessionEnd`` handler removed it) return
             ``False`` without performing any further work (Req 6.6).
          2. Drop the binding from the store first so any concurrent observer
             (e.g. a `/list` render or a racing ``SessionEnd``) immediately
             sees it gone.
          3. Tombstone discovery before any awaited cleanup: ``pid_dead`` as
             ended, ``idle_ttl_expired`` as unavailable.
          4. Clear pending permission, user-question, auto-approve, and hook state.
          5. Emit one INFO log including the reason and a fixed set of
             context fields. ``pid`` is always present in the log payload —
             rendered as an explicit ``None`` (JSON ``null``) when unknown,
             so consumers can distinguish "no pid recorded" from "field
             missing" (Req 8.3).

        The ``reason`` string is supplied by the caller (typically
        ``"pid_dead"`` or ``"idle_ttl_expired"``) and is logged verbatim;
        this method does not validate it.

        Returns ``True`` iff a binding was removed; ``False`` when the
        re-read guard saw the binding already gone.
        """
        current = self._binding_store.get_binding(session_id)
        if current is None:
            return False

        self._binding_store.remove_binding(session_id)

        async def run_async_cleanup(label: str, cleanup: Callable[[], Awaitable[object]]) -> None:
            try:
                await cleanup()
            except Exception:
                logger.exception("external binding cleanup step failed", extra={"session_id": session_id, "step": label})

        def run_sync_cleanup(label: str, cleanup: Callable[[], object]) -> None:
            try:
                cleanup()
            except Exception:
                logger.exception("external binding cleanup step failed", extra={"session_id": session_id, "step": label})

        if reason == "pid_dead":
            run_sync_cleanup("tombstone ended", lambda: self._tombstone.mark_ended(session_id))
            if self._external_discovery is not None:
                _discovery: ExternalSessionDiscoveryService = self._external_discovery
                run_sync_cleanup("external discovery cleanup", lambda: _discovery.remove_session(session_id))
            if self._permission_callback_registry is not None:
                _registry: PermissionCallbackRegistry = self._permission_callback_registry
                await run_async_cleanup(
                    "permission callback registry",
                    lambda: _registry.invalidate_session(session_id),
                )
            if self._external_uq_state is not None:
                _uq_state: ExternalUserQuestionState = self._external_uq_state
                run_sync_cleanup("external user question state", lambda: _uq_state.invalidate_session(session_id))
        elif reason == "idle_ttl_expired":
            run_sync_cleanup("tombstone unavailable", lambda: self._tombstone.mark_unavailable(session_id))
            if self._external_discovery is not None:
                _discovery = self._external_discovery
                run_sync_cleanup("external discovery cleanup", lambda: _discovery.remove_session(session_id))
        await run_async_cleanup("auto approve service", lambda: self._auto_approve_service.clear_session(session_id))
        await run_async_cleanup(
            "hook pending permissions",
            lambda: self._hook_socket_server.cancel_pending_permissions(session_id=session_id),
        )

        idle_hours = (utc_now() - current.last_activity_at).total_seconds() / 3600
        logger.info(
            "external binding removed",
            extra={
                "session_id": session_id,
                "user_id": current.user_id,
                "cwd": current.cwd,
                "bound_at": current.bound_at.isoformat(),
                "last_activity_at": current.last_activity_at.isoformat(),
                "idle_hours": idle_hours,
                "pid": current.pid,
                "reason": reason,
            },
        )
        return True
