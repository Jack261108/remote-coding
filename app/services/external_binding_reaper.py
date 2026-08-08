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
from datetime import datetime
from typing import TYPE_CHECKING

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.domain.session_tombstone import SessionTombstoneStore
from app.services.auto_approve_service import AutoApproveService
from app.services.external_binding_store import ExternalBindingStore

if TYPE_CHECKING:
    from app.adapters.claude.hook_socket_server import HookSocketServer
    from app.services.external_session_discovery import ExternalSessionDiscoveryService
    from app.services.external_user_question_state import ExternalUserQuestionState
    from app.services.permission_callback_registry import PermissionCallbackRegistry
    from app.services.unbound_permission_handler import UnboundPermissionHandler
    from app.services.user_question_callback_registry import UserQuestionCallbackRegistry

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
        unbound_permission_handler: UnboundPermissionHandler | None = None,
        external_uq_state: ExternalUserQuestionState | None = None,
        user_question_callback_registry: UserQuestionCallbackRegistry | None = None,
        external_discovery: ExternalSessionDiscoveryService | None = None,
        tombstone: SessionTombstoneStore | None = None,
        remove_callback: Callable[
            [str, str | None, datetime | None, int | None],
            Awaitable[ExternalBinding | None],
        ]
        | None = None,
    ) -> None:
        self._binding_store = binding_store
        self._auto_approve_service = auto_approve_service
        self._hook_socket_server = hook_socket_server
        self._permission_callback_registry = permission_callback_registry
        self._unbound_permission_handler = unbound_permission_handler
        self._external_uq_state = external_uq_state
        self._user_question_callback_registry = user_question_callback_registry
        self._external_discovery = external_discovery
        self._tombstone = tombstone or SessionTombstoneStore()
        self._remove_callback = remove_callback
        self._cleanup_in_progress: set[str] = set()

    def is_cleanup_in_progress(self, session_id: str) -> bool:
        return session_id in self._cleanup_in_progress

    async def remove_with_cleanup(
        self,
        session_id: str,
        *,
        reason: str,
        expected_binding_id: str | None = None,
        expected_last_activity_at: datetime | None = None,
        expected_pid: int | None = None,
    ) -> bool:
        """Atomically remove a binding and unwind its associated state."""
        if session_id in self._cleanup_in_progress:
            return False
        self._cleanup_in_progress.add(session_id)
        try:
            if self._remove_callback is not None:
                current = await self._remove_callback(
                    session_id,
                    expected_binding_id,
                    expected_last_activity_at,
                    expected_pid,
                )
            else:
                current = self._binding_store.get_binding(session_id)
                if current is not None and expected_binding_id is not None:
                    if current.binding_id != expected_binding_id:
                        current = None
                    elif expected_last_activity_at is not None and (
                        current.last_activity_at != expected_last_activity_at or current.pid != expected_pid
                    ):
                        current = None
                if current is not None:
                    self._binding_store.remove_binding(session_id)
            if current is None:
                return False

            async def run_async_cleanup(
                label: str,
                cleanup: Callable[[], Awaitable[object]],
            ) -> None:
                try:
                    await cleanup()
                except Exception:
                    logger.exception(
                        "external binding cleanup step failed",
                        extra={"session_id": session_id, "step": label},
                    )

            def run_sync_cleanup(
                label: str,
                cleanup: Callable[[], object],
            ) -> None:
                try:
                    cleanup()
                except Exception:
                    logger.exception(
                        "external binding cleanup step failed",
                        extra={"session_id": session_id, "step": label},
                    )

            clear_owner_state = reason in {"pid_dead", "manual_unbind"}
            if reason == "pid_dead":
                run_sync_cleanup(
                    "tombstone ended",
                    lambda: self._tombstone.mark_ended(session_id),
                )
                if self._external_discovery is not None:
                    discovery: ExternalSessionDiscoveryService = self._external_discovery
                    run_sync_cleanup(
                        "external discovery cleanup",
                        lambda: discovery.remove_session(session_id),
                    )
            elif reason == "idle_ttl_expired":
                run_sync_cleanup(
                    "tombstone unavailable",
                    lambda: self._tombstone.mark_unavailable(session_id),
                )
                if self._external_discovery is not None:
                    discovery = self._external_discovery
                    run_sync_cleanup(
                        "external discovery cleanup",
                        lambda: discovery.remove_session(session_id),
                    )

            if clear_owner_state and self._permission_callback_registry is not None:
                registry: PermissionCallbackRegistry = self._permission_callback_registry
                await run_async_cleanup(
                    "permission callback registry",
                    lambda: registry.invalidate_session(session_id),
                )
            if clear_owner_state and self._unbound_permission_handler is not None:
                unbound_handler: UnboundPermissionHandler = self._unbound_permission_handler
                await run_async_cleanup(
                    "unbound permission handler",
                    lambda: unbound_handler.invalidate_session(session_id),
                )
            if clear_owner_state and self._external_uq_state is not None:
                uq_state: ExternalUserQuestionState = self._external_uq_state
                run_sync_cleanup(
                    "external user question state",
                    lambda: uq_state.invalidate_session(session_id),
                )
            if clear_owner_state and self._user_question_callback_registry is not None:
                uq_registry: UserQuestionCallbackRegistry = self._user_question_callback_registry
                await run_async_cleanup(
                    "user question callback registry",
                    lambda: uq_registry.invalidate_session(session_id),
                )
            if reason == "pid_dead":
                await run_async_cleanup(
                    "auto approve service",
                    lambda: self._auto_approve_service.clear_session(session_id),
                )
            else:
                await run_async_cleanup(
                    "auto approve service",
                    lambda: self._auto_approve_service.clear_session(
                        session_id,
                        mark_ended=False,
                    ),
                )
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
        finally:
            self._cleanup_in_progress.discard(session_id)
