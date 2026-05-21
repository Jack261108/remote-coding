from __future__ import annotations

import logging
from collections.abc import Callable

from app.domain.session_models import SessionState
from app.services.session_state_cache import SessionStateCache
from app.services.session_state_repository import SessionStateRepository

logger = logging.getLogger(__name__)


def parse_user_question_key(question_key: str | None) -> tuple[str, int] | None:
    """Parse a user question key into (tool_use_id, index) tuple.

    The question_key format is "tool_use_id:index" where index is an integer.
    Returns None if the key is invalid, empty, or cannot be parsed.
    """
    if not question_key:
        return None
    tool_use_id, separator, index_text = str(question_key).rpartition(":")
    if not separator or not tool_use_id:
        return None
    try:
        return tool_use_id, int(index_text)
    except ValueError:
        return None


class StructuredReplyTracker:
    """Tracks which structured replies, permissions, and user questions have been emitted.

    Single responsibility: manage cursor state for structured reply/permission/question
    emission, delegating state retrieval to SessionStateCache and persistence to
    SessionStateRepository.
    """

    def __init__(
        self,
        cache: SessionStateCache,
        repository: SessionStateRepository,
        *,
        persist_fn: Callable[..., None] | None = None,
    ) -> None:
        self._cache = cache
        self._repository = repository
        self._persist_fn = persist_fn

    def get_structured_reply_cursor(self, session_id: str) -> tuple[str | None, str | None]:
        """Get the current structured reply and permission cursors for a session.

        Returns a tuple of (structured_reply_turn_id, structured_permission_key).
        Returns (None, None) if the session does not exist.
        """
        state = self._cache.get(session_id)
        if state is None:
            return None, None
        return state.structured_reply_turn_id, state.structured_permission_key

    def get_structured_user_question_cursor(self, session_id: str) -> str | None:
        """Get the current structured user question cursor for a session.

        Returns the structured_user_question_key or None if the session does not exist.
        """
        state = self._cache.get(session_id)
        if state is None:
            return None
        return state.structured_user_question_key

    def mark_structured_reply_emitted(self, session_id: str, *, turn_id: str) -> SessionState:
        """Mark a structured reply as emitted for the given session.

        If the turn_id is already the current cursor, no change is made.
        Creates a new session state if none exists.
        """
        state = self._cache.get(session_id)
        if state is None:
            state = self._cache.get_or_create(session_id=session_id)
            self._persist(state)
        if state.structured_reply_turn_id == turn_id:
            return state
        state.structured_reply_turn_id = turn_id
        self._persist(state)
        return state

    def mark_structured_permission_emitted(self, session_id: str, *, permission_key: str) -> SessionState:
        """Mark a structured permission as emitted for the given session.

        If the permission_key is already the current cursor, no change is made.
        Creates a new session state if none exists.
        """
        state = self._cache.get(session_id)
        if state is None:
            state = self._cache.get_or_create(session_id=session_id)
            self._persist(state)
        if state.structured_permission_key == permission_key:
            return state
        state.structured_permission_key = permission_key
        self._persist(state)
        return state

    def mark_structured_user_question_emitted(self, session_id: str, *, question_key: str) -> SessionState:
        """Mark a structured user question as emitted for the given session.

        If the question_key is already the current cursor, no change is made.
        If the new question has the same tool_use_id but a lower index than the
        current one, the update is skipped (prevents backward cursor movement).
        Creates a new session state if none exists.
        """
        state = self._cache.get(session_id)
        if state is None:
            state = self._cache.get_or_create(session_id=session_id)
            self._persist(state)
        if state.structured_user_question_key == question_key:
            return state
        current_parsed = parse_user_question_key(state.structured_user_question_key)
        next_parsed = parse_user_question_key(question_key)
        if (
            current_parsed is not None
            and next_parsed is not None
            and current_parsed[0] == next_parsed[0]
            and next_parsed[1] < current_parsed[1]
        ):
            return state
        state.structured_user_question_key = question_key
        self._persist(state)
        return state

    def _persist(self, state: SessionState) -> None:
        """Persist state changes via the configured persist function."""
        if self._persist_fn is not None:
            self._persist_fn(state, publish=False)
        else:
            self._cache.put(state)
            self._repository.save_checkpoint(state.session_id, state.checkpoint)
            self._repository.save(state)
