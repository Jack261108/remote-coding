from __future__ import annotations

import logging

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.session_models import (
    ConversationTurn,
    ParserCheckpoint,
    SessionState,
)

logger = logging.getLogger(__name__)


class SessionStateRepository:
    """Persistence layer for SessionState objects.

    Wraps FileSessionStore with hydration logic (merging checkpoint + conversation
    into SessionState). Single responsibility: load/save/list persisted session states.
    """

    def __init__(self, file_store: FileSessionStore) -> None:
        self._file_store = file_store

    def load(self, session_id: str) -> SessionState | None:
        """Load a SessionState from persistence, hydrating with checkpoint and conversation.

        Returns None if the session does not exist or the file is corrupted.
        """
        try:
            state = self._file_store.load_session_state(session_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Failed to load session state for %s: %s", session_id, exc)
            return None

        if state is None:
            return None

        return self._hydrate(state)

    def save(self, state: SessionState) -> None:
        """Persist a SessionState (including conversation) to file storage."""
        self._file_store.save_session_state(state)

    def save_checkpoint(self, session_id: str, checkpoint: ParserCheckpoint) -> None:
        """Persist a parser checkpoint for the given session."""
        self._file_store.save_checkpoint(session_id, checkpoint)

    def load_checkpoint(self, session_id: str) -> ParserCheckpoint:
        """Load the parser checkpoint for the given session.

        Returns a default ParserCheckpoint if none exists.
        """
        return self._file_store.load_checkpoint(session_id)

    def load_conversation(self, session_id: str) -> list[ConversationTurn]:
        """Load the conversation turns for the given session.

        Returns an empty list if no conversation exists.
        """
        return self._file_store.load_conversation(session_id)

    def list_states(self) -> list[SessionState]:
        """List all persisted session states, hydrating each with checkpoint and conversation."""
        results: list[SessionState] = []
        for state in self._file_store.list_session_states():
            try:
                results.append(self._hydrate(state))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "Failed to hydrate session state for %s: %s",
                    state.session_id,
                    exc,
                )
                continue
        return results

    def _hydrate(self, state: SessionState) -> SessionState:
        """Merge checkpoint and conversation data into a SessionState.

        This is the hydration logic extracted from SessionStore._hydrate_and_cache_state,
        but without any caching concerns. It simply loads the checkpoint and conversation
        from the file store and merges them into the state object.
        """
        loaded_checkpoint = self._file_store.load_checkpoint(state.session_id)
        loaded_turns = state.turns or self._file_store.load_conversation(state.session_id)

        state.checkpoint = loaded_checkpoint
        if not state.turns:
            state.turns = loaded_turns
        state.history_loaded = bool(state.turns or state.tool_calls or state.pending_permission is not None)
        return state
