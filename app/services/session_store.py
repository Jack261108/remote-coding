from __future__ import annotations

from datetime import datetime

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.hook_models import validate_session_id
from app.domain.session_models import (
    ParserCheckpoint,
    SessionPhase,
    SessionState,
    is_claude_session_id,
    parse_user_question_key,
)
from app.services.session_event_processor import SessionEventProcessor
from app.services.session_lookup_service import SessionLookupService
from app.services.session_notifier import SessionNotifier
from app.services.session_state_cache import SessionStateCache
from app.services.session_state_repository import SessionStateRepository
from app.services.structured_reply_tracker import StructuredReplyTracker

# Re-export for backward compatibility
__all__ = ["SessionStore", "SessionStoreFacade", "is_claude_session_id", "parse_user_question_key", "persist_session_state"]


def persist_session_state(
    state: SessionState,
    cache: SessionStateCache,
    repository: SessionStateRepository,
    notifier: SessionNotifier,
    *,
    publish: bool = True,
) -> None:
    """持久化 SessionState 到缓存和仓库。"""
    if publish:
        state.revision += 1
    cache.put(state)
    repository.save_checkpoint(state.session_id, state.checkpoint)
    repository.save(state)
    if publish:
        notifier.publish(state.session_id, state)


class SessionStoreFacade:
    """Backward-compatible facade delegating to new components."""

    def __init__(self, file_store: FileSessionStore) -> None:
        self._file_store = file_store
        self._repository = SessionStateRepository(file_store)
        self._cache = SessionStateCache(self._repository)
        self._notifier = SessionNotifier()
        self._event_processor = SessionEventProcessor(self._cache, self._repository, self._notifier)
        self._lookup = SessionLookupService(self._cache, self._repository, persist_fn=self._persist)
        self._tracker = StructuredReplyTracker(self._cache, self._repository, persist_fn=self._persist)

    def process(self, event) -> SessionState:
        return self._event_processor.process(event)

    # ─── Delegated lookup/resolution methods ───────────────────────

    def find_by_terminal_id(self, terminal_id: str | None) -> SessionState | None:
        return self._lookup.find_by_terminal_id(terminal_id)

    def find_by_pending_tool_use_id(self, tool_use_id: str | None) -> SessionState | None:
        return self._lookup.find_by_pending_tool_use_id(tool_use_id)

    def find_by_active_user_question_tool_use_id(self, tool_use_id: str | None) -> SessionState | None:
        return self._lookup.find_by_active_user_question_tool_use_id(tool_use_id)

    def find_by_active_user_question_key(self, question_key: str | None) -> SessionState | None:
        return self._lookup.find_by_active_user_question_key(question_key)

    def find_by_user_turn_text(
        self,
        *,
        user_id: int,
        workdir: str,
        text: str,
        since: datetime,
        until: datetime | None = None,
        terminal_id: str | None = None,
    ) -> SessionState | None:
        return self._lookup.find_by_user_turn_text(
            user_id=user_id,
            workdir=workdir,
            text=text,
            since=since,
            until=until,
            terminal_id=terminal_id,
        )

    def resolve_interactive_session_id(
        self,
        *,
        terminal_id: str | None,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
        require_claude_session: bool = False,
    ) -> str | None:
        return self._lookup.resolve_interactive_session_id(
            terminal_id=terminal_id,
            claude_session_id=claude_session_id,
            fallback_session_id=fallback_session_id,
            require_claude_session=require_claude_session,
        )

    def get_interactive_state(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
        require_claude_session: bool = False,
    ) -> SessionState | None:
        return self._lookup.get_interactive_state(
            terminal_id=terminal_id,
            workdir=workdir,
            claude_session_id=claude_session_id,
            fallback_session_id=fallback_session_id,
            require_claude_session=require_claude_session,
        )

    def mark_interactive_turn_processing(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> SessionState | None:
        return self._lookup.mark_interactive_turn_processing(
            terminal_id=terminal_id,
            workdir=workdir,
            claude_session_id=claude_session_id,
            fallback_session_id=fallback_session_id,
        )

    def interactive_completion_phase(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> SessionPhase | None:
        return self._lookup.interactive_completion_phase(
            terminal_id=terminal_id,
            workdir=workdir,
            claude_session_id=claude_session_id,
            fallback_session_id=fallback_session_id,
        )

    def latest_completed_assistant_turn_id(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> str | None:
        return self._lookup.latest_completed_assistant_turn_id(
            terminal_id=terminal_id,
            workdir=workdir,
            claude_session_id=claude_session_id,
            fallback_session_id=fallback_session_id,
        )

    # ─── Core store methods ────────────────────────────────────────

    def get_or_create(
        self,
        *,
        session_id: str,
        user_id: int | None = None,
        provider: str = "claude_code",
        workdir: str = ".",
        terminal_id: str | None = None,
        claude_session_id: str | None = None,
    ) -> SessionState:
        state = self._cache.get_or_create(
            session_id=session_id,
            user_id=user_id,
            provider=provider,
            workdir=workdir,
            terminal_id=terminal_id,
            claude_session_id=claude_session_id,
        )
        self._persist(state, publish=False)
        return state

    def get(self, session_id: str) -> SessionState | None:
        session_id = validate_session_id(session_id)
        state = self._cache.get(session_id)
        if state is not None:
            return state
        loaded = self._file_store.load_session_state(session_id)
        if loaded is None:
            return None
        return self._cache.hydrate_and_cache(loaded)

    def values(self) -> list[SessionState]:
        """Return all currently cached session states (snapshot)."""
        return self._cache.values()

    def save(self, state: SessionState, *, publish: bool = True) -> None:
        """Persist and optionally publish a session state change."""
        self._persist(state, publish=publish)

    def get_cursor(self, session_id: str) -> int:
        session_id = validate_session_id(session_id)
        state = self._cache.get(session_id)
        if state is not None:
            return state.revision
        loaded = self._file_store.load_session_state(session_id)
        if loaded is None:
            return 0
        self._cache.put(loaded)
        return loaded.revision

    def get_revision(self, session_id: str) -> int:
        return self.get_cursor(session_id)

    def get_structured_reply_cursor(self, session_id: str) -> tuple[str | None, str | None]:
        return self._tracker.get_structured_reply_cursor(session_id)

    def get_structured_user_question_cursor(self, session_id: str) -> str | None:
        return self._tracker.get_structured_user_question_cursor(session_id)

    def mark_structured_reply_emitted(self, session_id: str, *, turn_id: str) -> SessionState:
        return self._tracker.mark_structured_reply_emitted(session_id, turn_id=turn_id)

    def mark_structured_permission_emitted(self, session_id: str, *, permission_key: str) -> SessionState:
        return self._tracker.mark_structured_permission_emitted(session_id, permission_key=permission_key)

    def mark_structured_user_question_emitted(self, session_id: str, *, question_key: str) -> SessionState:
        return self._tracker.mark_structured_user_question_emitted(session_id, question_key=question_key)

    async def wait_for_publish(self, session_id: str, *, since_cursor: int, timeout_sec: float) -> bool:
        return await self._notifier.wait_for_publish(session_id, since_cursor=since_cursor, timeout_sec=timeout_sec)

    async def wait_for_change(self, session_id: str, *, since_revision: int, timeout_sec: float) -> bool:
        return await self._notifier.wait_for_change(session_id, since_revision=since_revision, timeout_sec=timeout_sec)

    def save_checkpoint(self, session_id: str, checkpoint: ParserCheckpoint) -> SessionState:
        session_id = validate_session_id(session_id)
        state = self._cache.get(session_id)
        if state is None:
            raise KeyError(f"Session {session_id} not found in cache")
        state.checkpoint = checkpoint
        self._persist(state, publish=False)
        return state

    def _persist(self, state: SessionState, *, publish: bool = True) -> None:
        persist_session_state(state, self._cache, self._repository, self._notifier, publish=publish)

    def cleanup_stale_sessions(self, max_age_hours: int = 24) -> int:
        """Clean up session directories for sessions that have ended and are older than max_age_hours.

        Returns:
            Number of sessions deleted.
        """
        return self._file_store.cleanup_stale_sessions(max_age_hours=max_age_hours)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session directory and all its contents.

        Returns:
            True if the session was deleted, False if it didn't exist.
        """
        # Remove from cache
        self._cache.remove(session_id)
        # Delete from disk
        return self._file_store.delete_session(session_id)


# Backward-compat alias
SessionStore = SessionStoreFacade
