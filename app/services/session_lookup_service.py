from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.domain.hook_models import validate_session_id
from app.domain.session_models import (
    SessionPhase,
    SessionState,
    ToolStatus,
    is_claude_session_id,
    parse_user_question_key,
)
from app.domain.user_question_models import extract_user_question_prompts
from app.services.session_state_cache import SessionStateCache
from app.services.session_state_repository import SessionStateRepository


def _normalize_turn_match_text(text: str) -> str:
    """Normalize whitespace in turn text for comparison."""
    return " ".join(text.replace("\r\n", "\n").replace("\r", "\n").split())


def _same_workdir(a: str | None, b: str | None) -> bool:
    """Compare two workdir paths for equality after resolving."""
    if not a or not b:
        return a == b
    return Path(a).resolve() == Path(b).resolve()


class SessionLookupService:
    """All find_by_* methods plus interactive session resolution.

    Queries the SessionStateCache first, then falls back to
    SessionStateRepository for persisted states.
    """

    def __init__(
        self,
        cache: SessionStateCache,
        repository: SessionStateRepository,
        persist_fn: Callable[[SessionState], None] | None = None,
    ) -> None:
        self._cache = cache
        self._repository = repository
        self._persist_fn = persist_fn

    # ─── Ranking helpers ───────────────────────────────────────────

    def _is_claude_state(self, state: SessionState | None) -> bool:
        if state is None:
            return False
        return is_claude_session_id(state.claude_session_id) or is_claude_session_id(state.session_id)

    def _state_rank(self, state: SessionState) -> tuple[int, int, int, float, int, float, int]:
        has_content = int(bool(state.turns or state.tool_calls or state.pending_permission is not None))
        has_pending_permission = int(state.pending_permission is not None or state.phase == SessionPhase.WAITING_FOR_APPROVAL)
        is_active = int(state.phase in {SessionPhase.WAITING_FOR_APPROVAL, SessionPhase.PROCESSING, SessionPhase.COMPACTING})
        is_claude = int(self._is_claude_state(state))
        created_at = state.created_at.timestamp()
        last_activity = state.last_activity.timestamp()
        return (
            is_claude,
            has_pending_permission,
            has_content,
            last_activity,
            is_active,
            created_at,
            state.revision,
        )

    def _explicit_resolution_rank(self, state: SessionState) -> tuple[int, int, float, float, int]:
        has_content = int(bool(state.turns or state.tool_calls or state.pending_permission is not None))
        has_pending_permission = int(state.pending_permission is not None or state.phase == SessionPhase.WAITING_FOR_APPROVAL)
        created_at = state.created_at.timestamp()
        last_activity = state.last_activity.timestamp()
        return (
            has_pending_permission,
            has_content,
            last_activity,
            created_at,
            state.revision,
        )

    # ─── Matcher helpers ───────────────────────────────────────────

    def _has_pending_permission_tool_use_id(self, state: SessionState, tool_use_id: str) -> bool:
        pending = state.pending_permission
        return pending is not None and pending.tool_use_id == tool_use_id

    def _has_active_user_question_tool_use_id(self, state: SessionState, tool_use_id: str) -> bool:
        pending = state.pending_permission
        if pending is not None and pending.tool_use_id == tool_use_id:
            prompts = extract_user_question_prompts(
                tool_use_id=pending.tool_use_id,
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
            )
            if prompts:
                return True
        tool = state.tool_calls.get(tool_use_id)
        if tool is None or tool.status not in {ToolStatus.RUNNING, ToolStatus.WAITING_FOR_APPROVAL}:
            return False
        prompts = extract_user_question_prompts(
            tool_use_id=tool.tool_use_id,
            tool_name=tool.name,
            tool_input=tool.input,
        )
        return bool(prompts)

    def _has_active_user_question_key(self, state: SessionState, question_key: str) -> bool:
        if not question_key:
            return False
        parsed = parse_user_question_key(question_key)
        if parsed is None:
            return False
        tool_use_id, _ = parsed
        if not self._has_active_user_question_tool_use_id(state, tool_use_id):
            return False

        pending = state.pending_permission
        if pending is not None and pending.tool_use_id == tool_use_id:
            prompts = extract_user_question_prompts(
                tool_use_id=pending.tool_use_id,
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
            )
            if any(prompt.key == question_key for prompt in prompts):
                return True

        tool = state.tool_calls.get(tool_use_id)
        if tool is None or tool.status not in {ToolStatus.RUNNING, ToolStatus.WAITING_FOR_APPROVAL}:
            return False
        prompts = extract_user_question_prompts(
            tool_use_id=tool.tool_use_id,
            tool_name=tool.name,
            tool_input=tool.input,
        )
        return any(prompt.key == question_key for prompt in prompts)

    # ─── Generic cached-or-persisted search ────────────────────────

    def _find_cached_or_persisted_state(
        self,
        *,
        matcher: Callable[[SessionState], bool],
    ) -> SessionState | None:
        best: SessionState | None = None
        for state in self._cache.values():
            if not matcher(state):
                continue
            if best is None or self._state_rank(state) > self._state_rank(best):
                best = state
        if best is not None:
            return best

        for state in self._repository.list_states():
            if not matcher(state):
                continue
            candidate = self._cache.hydrate_and_cache(state)
            if not matcher(candidate):
                continue
            if best is None or self._state_rank(candidate) > self._state_rank(best):
                best = candidate
        return best

    # ─── find_by_* methods ─────────────────────────────────────────

    def find_by_terminal_id(self, terminal_id: str | None) -> SessionState | None:
        if not terminal_id:
            return None

        best: SessionState | None = None
        for state in self._cache.values():
            if state.terminal_id != terminal_id:
                continue
            if best is None or self._state_rank(state) > self._state_rank(best):
                best = state
        if best is not None and self._state_rank(best)[:4] == (1, 1, 1, 1):
            return best

        for state in self._repository.list_states():
            if state.terminal_id != terminal_id:
                continue
            candidate = self._cache.hydrate_and_cache(state)
            if candidate.terminal_id != terminal_id:
                continue
            if best is None or self._state_rank(candidate) > self._state_rank(best):
                best = candidate
        return best

    def find_by_pending_tool_use_id(self, tool_use_id: str | None) -> SessionState | None:
        if not tool_use_id:
            return None
        return self._find_cached_or_persisted_state(
            matcher=lambda state: self._has_pending_permission_tool_use_id(state, tool_use_id),
        )

    def find_by_active_user_question_tool_use_id(self, tool_use_id: str | None) -> SessionState | None:
        if not tool_use_id:
            return None
        return self._find_cached_or_persisted_state(
            matcher=lambda state: self._has_active_user_question_tool_use_id(state, tool_use_id),
        )

    def find_by_active_user_question_key(self, question_key: str | None) -> SessionState | None:
        if not question_key:
            return None
        return self._find_cached_or_persisted_state(
            matcher=lambda state: self._has_active_user_question_key(state, question_key),
        )

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
        normalized_text = _normalize_turn_match_text(text)
        if not normalized_text:
            return None

        best: tuple[datetime, int, SessionState] | None = None
        seen: set[str] = set()
        for state in [*self._cache.values(), *self._repository.list_states()]:
            if state.session_id in seen:
                continue
            seen.add(state.session_id)
            candidate = self._cache.hydrate_and_cache(state)
            if candidate.user_id is not None and candidate.user_id != user_id:
                continue
            if not _same_workdir(candidate.workdir, workdir):
                continue
            if terminal_id and candidate.terminal_id != terminal_id:
                continue
            matched_at = self._latest_matching_user_turn_at(candidate, normalized_text=normalized_text, since=since, until=until)
            if matched_at is None:
                continue
            ranked = (matched_at, candidate.revision, candidate)
            if best is None or ranked[:2] > best[:2]:
                best = ranked
        return best[2] if best is not None else None

    def _latest_matching_user_turn_at(
        self,
        state: SessionState,
        *,
        normalized_text: str,
        since: datetime,
        until: datetime | None,
    ) -> datetime | None:
        matched_at: datetime | None = None
        for turn in state.turns:
            if turn.role != "user":
                continue
            if turn.started_at < since:
                continue
            if until is not None and turn.started_at > until:
                continue
            if _normalize_turn_match_text(turn.text) != normalized_text:
                continue
            if matched_at is None or turn.started_at > matched_at:
                matched_at = turn.started_at
        return matched_at

    # ─── Interactive session resolution ────────────────────────────

    def resolve_interactive_session_id(
        self,
        *,
        terminal_id: str | None,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
        require_claude_session: bool = False,
    ) -> str | None:
        bound = self.find_by_terminal_id(terminal_id)
        if is_claude_session_id(claude_session_id):
            explicit = self._get(claude_session_id)
            if self._is_claude_state(bound) and bound is not None and bound.session_id != claude_session_id:
                if explicit is None or self._explicit_resolution_rank(bound) > self._explicit_resolution_rank(explicit):
                    return bound.session_id
            return claude_session_id
        if bound is not None and self._is_claude_state(bound):
            return bound.session_id
        if require_claude_session:
            return None
        return fallback_session_id or claude_session_id

    def get_interactive_state(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
        require_claude_session: bool = False,
    ) -> SessionState | None:
        session_id = self.resolve_interactive_session_id(
            terminal_id=terminal_id,
            claude_session_id=claude_session_id,
            fallback_session_id=fallback_session_id,
            require_claude_session=require_claude_session,
        )
        if session_id is None:
            return None
        return self._cache.get_or_create(
            session_id=session_id,
            provider="claude_code",
            workdir=workdir,
            terminal_id=terminal_id,
        )

    def mark_interactive_turn_processing(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> SessionState | None:
        state = self.get_interactive_state(
            terminal_id=terminal_id,
            workdir=workdir,
            claude_session_id=claude_session_id,
            fallback_session_id=fallback_session_id,
        )
        if state is None:
            return None
        if state.phase in {SessionPhase.IDLE, SessionPhase.WAITING_FOR_INPUT, SessionPhase.ENDED}:
            state.phase = SessionPhase.PROCESSING
            if self._persist_fn is not None:
                self._persist_fn(state)
        return state

    def interactive_completion_phase(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> SessionPhase | None:
        state = self.get_interactive_state(
            terminal_id=terminal_id,
            workdir=workdir,
            claude_session_id=claude_session_id,
            fallback_session_id=fallback_session_id,
            require_claude_session=True,
        )
        if state is None:
            return None
        if state.phase in {SessionPhase.WAITING_FOR_INPUT, SessionPhase.ENDED}:
            return state.phase
        return None

    def latest_completed_assistant_turn_id(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> str | None:
        state = self.get_interactive_state(
            terminal_id=terminal_id,
            workdir=workdir,
            claude_session_id=claude_session_id,
            fallback_session_id=fallback_session_id,
            require_claude_session=True,
        )
        if state is None:
            return None
        for turn in reversed(state.turns):
            if turn.role == "assistant" and turn.is_complete:
                return turn.turn_id
        return None

    # ─── Internal helpers ──────────────────────────────────────────

    def _get(self, session_id: str | None) -> SessionState | None:
        """Get a session state from cache (with repository fallback)."""
        if not session_id:
            return None
        session_id = validate_session_id(session_id)
        return self._cache.get(session_id)

    def get_cursor(self, session_id: str) -> int:
        """Return the revision cursor for *session_id* from the cache.

        Falls back to the repository if not cached. Returns 0 if unknown.
        This matches the old SessionStore.get_cursor behavior.
        """
        session_id = validate_session_id(session_id)
        state = self._cache.get(session_id)
        if state is not None:
            return state.revision
        loaded = self._repository.load(session_id)
        if loaded is None:
            return 0
        self._cache.put(loaded)
        return loaded.revision
