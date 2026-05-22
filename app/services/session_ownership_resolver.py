"""Session Ownership Resolver — first gate in the hook pipeline.

Determines who owns a session based on explicit ownership data only.
No workdir heuristics for external sessions.
"""

from __future__ import annotations

from app.domain.external_session_models import OwnershipResult, SessionOrigin
from app.services.external_binding_store import ExternalBindingStore
from app.services.session_service import SessionService


class SessionOwnershipResolver:
    """Resolve ownership of a Claude session by priority chain.

    Priority:
    1. Tmux-owned: session_id matches a SessionContext.claude_session_id
       where the context has a terminal_id (tmux-launched).
    2. External-bound: session_id exists in ExternalBindingStore.
    3. Unbound: none of the above.

    The resolver NEVER uses workdir-based matching for sessions without
    a terminal_id (external sessions).
    """

    def __init__(
        self,
        *,
        session_service: SessionService,
        binding_store: ExternalBindingStore,
    ) -> None:
        self._session_service = session_service
        self._binding_store = binding_store

    async def resolve(self, session_id: str) -> OwnershipResult:
        """Determine ownership of a session_id.

        Priority:
        1. Explicit tmux owner: session_id matches a SessionContext.claude_session_id
           where the context has a terminal_id (tmux-launched)
        2. External binding: session_id exists in ExternalBindingStore
        3. Unbound: none of the above

        NO workdir-based matching is performed here for external sessions.
        """
        # Priority 1: Check if tmux-owned
        all_contexts = await self._session_service.list_all()
        for ctx in all_contexts:
            if ctx.claude_session_id == session_id and ctx.terminal_id is not None:
                return OwnershipResult(
                    owner_user_id=ctx.user_id,
                    origin=SessionOrigin.TMUX,
                    ownership_state="owned",
                )

        # Priority 2: Check external binding
        binding = self._binding_store.get_binding(session_id)
        if binding is not None:
            return OwnershipResult(
                owner_user_id=binding.user_id,
                origin=SessionOrigin.EXTERNAL,
                ownership_state="bound",
            )

        # Priority 3: Unbound
        return OwnershipResult(
            owner_user_id=None,
            origin=SessionOrigin.EXTERNAL,
            ownership_state="unbound",
        )

    async def is_tmux_owned(self, session_id: str) -> bool:
        """Quick check if session is tmux-owned (has terminal_id)."""
        all_contexts = await self._session_service.list_all()
        return any(ctx.claude_session_id == session_id and ctx.terminal_id is not None for ctx in all_contexts)
