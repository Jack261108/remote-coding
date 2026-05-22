from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.domain.external_session_models import BindResult, ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_discovery import ExternalSessionDiscoveryService

logger = logging.getLogger(__name__)


def _resolve_jsonl_path(*, session_id: str, cwd: str, projects_dir: Path) -> Path:
    """Resolve the JSONL path for a session using Claude's path convention.

    Convention: ~/.claude/projects/<sanitized_cwd>/<session_id>.jsonl
    where sanitized_cwd replaces '/' with '-' and '.' with '-'.
    """
    project_dir = cwd.replace("/", "-").replace(".", "-")
    return projects_dir / project_dir / f"{session_id}.jsonl"


class ExternalSessionBinder:
    """Handles binding/unbinding of external sessions to Telegram users."""

    def __init__(
        self,
        *,
        discovery: ExternalSessionDiscoveryService,
        binding_store: ExternalBindingStore,
        projects_dir: Path,
        sync_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._discovery = discovery
        self._binding_store = binding_store
        self._projects_dir = projects_dir
        self._sync_callback = sync_callback

    async def bind(self, *, user_id: int, session_id: str) -> BindResult:
        """Bind an unbound session to a user.

        Steps:
        1. Verify session exists in discovery list
        2. Verify not already bound to another user
        3. Resolve JSONL path immediately
        4. Create binding in store
        5. Remove from discovery list
        6. Call sync_callback to trigger JSONL parsing
        7. Return BindResult with jsonl_path and whether file exists
        """
        # 1. Verify session exists in discovery list
        unbound = self._discovery.get(session_id)
        if unbound is None:
            return BindResult(
                success=False,
                message="Session not found in discoverable list",
                session_id=session_id,
            )

        # 2. Verify not already bound to another user
        existing = self._binding_store.get_binding(session_id)
        if existing is not None:
            if existing.user_id == user_id:
                return BindResult(
                    success=False,
                    message="Session is already bound to you",
                    session_id=session_id,
                )
            return BindResult(
                success=False,
                message="Session already bound to another user",
                session_id=session_id,
            )

        # 3. Resolve JSONL path
        jsonl_path = _resolve_jsonl_path(
            session_id=session_id,
            cwd=unbound.cwd,
            projects_dir=self._projects_dir,
        )

        # 4. Create binding in store
        binding = ExternalBinding(
            session_id=session_id,
            user_id=user_id,
            cwd=unbound.cwd,
            bound_at=utc_now(),
            jsonl_path=str(jsonl_path),
        )
        self._binding_store.save_binding(binding)

        # 5. Remove from discovery list
        self._discovery.remove_session(session_id)

        # 6. Call sync_callback to trigger JSONL parsing
        if self._sync_callback is not None:
            try:
                await self._sync_callback(session_id, unbound.cwd)
            except Exception:
                logger.exception("sync_callback failed for session %s", session_id)

        # 7. Return result
        file_exists = jsonl_path.exists()
        return BindResult(
            success=True,
            message="Session bound successfully",
            session_id=session_id,
            jsonl_path=jsonl_path,
            conversation_available=file_exists,
        )

    async def unbind(self, *, user_id: int, session_id: str) -> BindResult:
        """Unbind a session from a user.

        If the session is still active (exists in recent events), return it
        to the discovery list.
        """
        # 1. Verify session is bound to this user
        binding = self._binding_store.get_binding(session_id)
        if binding is None:
            return BindResult(
                success=False,
                message="Session not bound to you",
                session_id=session_id,
            )
        if binding.user_id != user_id:
            return BindResult(
                success=False,
                message="Session not bound to you",
                session_id=session_id,
            )

        # 2. Remove binding from store
        self._binding_store.remove_binding(session_id)

        # 3. Note: The session will be re-discovered automatically when
        #    the next hook event arrives (if still alive). We don't need
        #    to manually add it back to discovery since the ownership resolver
        #    will route it to discovery on the next event.

        return BindResult(
            success=True,
            message="Session unbound successfully",
            session_id=session_id,
        )

    def get_binding_user(self, session_id: str) -> int | None:
        """Get the user_id that owns a bound session, or None."""
        binding = self._binding_store.get_binding(session_id)
        return binding.user_id if binding else None

    def list_bound_for_user(self, user_id: int) -> list[ExternalBinding]:
        """List all external sessions bound to a specific user."""
        return self._binding_store.get_bindings_for_user(user_id)
