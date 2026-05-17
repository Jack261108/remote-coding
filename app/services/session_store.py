from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.user_question_models import extract_user_question_prompts
from app.domain.hook_models import validate_session_id
from app.domain.session_models import (
    ParserCheckpoint,
    SessionPhase,
    SessionState,
    ToolStatus,
)
from app.services.session_event_processing import SessionEventProcessingMixin


CLAUDE_SESSION_PREFIX = "claude-session-"
_UUID_SESSION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def is_claude_session_id(session_id: str | None) -> bool:
    if not session_id:
        return False
    text = str(session_id).strip()
    if not text:
        return False
    return text.startswith(CLAUDE_SESSION_PREFIX) or bool(_UUID_SESSION_RE.match(text))


def parse_user_question_key(question_key: str | None) -> tuple[str, int] | None:
    if not question_key:
        return None
    tool_use_id, separator, index_text = str(question_key).rpartition(":")
    if not separator or not tool_use_id:
        return None
    try:
        return tool_use_id, int(index_text)
    except ValueError:
        return None


def _normalize_turn_match_text(text: str) -> str:
    return " ".join(text.replace("\r\n", "\n").replace("\r", "\n").split())


class SessionStore(SessionEventProcessingMixin):
    def __init__(self, file_store: FileSessionStore) -> None:
        self._file_store = file_store
        self._states: dict[str, SessionState] = {}
        self._revision_conditions: dict[str, asyncio.Condition] = {}

    def _is_claude_session_id(self, session_id: str | None) -> bool:
        return is_claude_session_id(session_id)

    def _is_claude_state(self, state: SessionState | None) -> bool:
        if state is None:
            return False
        return self._is_claude_session_id(state.claude_session_id) or self._is_claude_session_id(state.session_id)

    def _state_rank(self, state: SessionState) -> tuple[int, int, int, float, int, float, int]:
        has_content = int(bool(state.turns or state.tool_calls or state.pending_permission is not None))
        has_pending_permission = int(state.pending_permission is not None or state.phase == SessionPhase.WAITING_FOR_APPROVAL)
        is_active = int(state.phase in {SessionPhase.WAITING_FOR_APPROVAL, SessionPhase.PROCESSING, SessionPhase.COMPACTING})
        is_claude = int(self._is_claude_state(state))
        created_at = state.created_at.timestamp()
        last_activity = state.last_activity.timestamp()
        return is_claude, has_pending_permission, has_content, last_activity, is_active, created_at, state.revision

    def _explicit_resolution_rank(self, state: SessionState) -> tuple[int, int, float, float, int]:
        has_content = int(bool(state.turns or state.tool_calls or state.pending_permission is not None))
        has_pending_permission = int(state.pending_permission is not None or state.phase == SessionPhase.WAITING_FOR_APPROVAL)
        created_at = state.created_at.timestamp()
        last_activity = state.last_activity.timestamp()
        return has_pending_permission, has_content, last_activity, created_at, state.revision

    def find_by_terminal_id(self, terminal_id: str | None) -> SessionState | None:
        if not terminal_id:
            return None

        best: SessionState | None = None
        for state in self._states.values():
            if state.terminal_id != terminal_id:
                continue
            if best is None or self._state_rank(state) > self._state_rank(best):
                best = state
        if best is not None and self._state_rank(best)[:4] == (1, 1, 1, 1):
            return best

        for state in self._file_store.list_session_states():
            if state.terminal_id != terminal_id:
                continue
            candidate = self._hydrate_and_cache_state(state)
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
        for state in [*self._states.values(), *self._file_store.list_session_states()]:
            if state.session_id in seen:
                continue
            seen.add(state.session_id)
            candidate = self._hydrate_and_cache_state(state)
            if candidate.user_id is not None and candidate.user_id != user_id:
                continue
            if str(Path(candidate.workdir).resolve()) != str(Path(workdir).resolve()):
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

    def _hydrate_and_cache_state(self, state: SessionState) -> SessionState:
        loaded_checkpoint = self._file_store.load_checkpoint(state.session_id)
        loaded_turns = state.turns or self._file_store.load_conversation(state.session_id)
        cached = self._states.get(state.session_id)
        if cached is not None:
            if not cached.turns and loaded_turns:
                cached.turns = loaded_turns
            if not cached.tool_calls and state.tool_calls:
                cached.tool_calls = state.tool_calls
            if cached.pending_permission is None and state.pending_permission is not None:
                cached.pending_permission = state.pending_permission
            if cached.user_id is None and state.user_id is not None:
                cached.user_id = state.user_id
            if cached.terminal_id is None and state.terminal_id is not None:
                cached.terminal_id = state.terminal_id
            if cached.claude_session_id is None and state.claude_session_id is not None:
                cached.claude_session_id = state.claude_session_id
            if cached.checkpoint.last_offset == 0 and loaded_checkpoint.last_offset != 0:
                cached.checkpoint = loaded_checkpoint
            cached.history_loaded = self._has_loaded_history(cached)
            return cached

        state.checkpoint = loaded_checkpoint
        if not state.turns:
            state.turns = loaded_turns
        state.history_loaded = self._has_loaded_history(state)
        self._states[state.session_id] = state
        return state

    def _has_loaded_history(self, state: SessionState) -> bool:
        return bool(state.turns or state.tool_calls or state.pending_permission is not None)

    def _find_cached_or_persisted_state(
        self,
        *,
        matcher: Callable[[SessionState], bool],
    ) -> SessionState | None:
        best: SessionState | None = None
        for state in self._states.values():
            if not matcher(state):
                continue
            if best is None or self._state_rank(state) > self._state_rank(best):
                best = state
        if best is not None:
            return best

        for state in self._file_store.list_session_states():
            if not matcher(state):
                continue
            candidate = self._hydrate_and_cache_state(state)
            if not matcher(candidate):
                continue
            if best is None or self._state_rank(candidate) > self._state_rank(best):
                best = candidate
        return best

    def resolve_interactive_session_id(
        self,
        *,
        terminal_id: str | None,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
        require_claude_session: bool = False,
    ) -> str | None:
        bound = self.find_by_terminal_id(terminal_id)
        if self._is_claude_session_id(claude_session_id):
            explicit = self.get(claude_session_id)
            if self._is_claude_state(bound) and bound is not None and bound.session_id != claude_session_id:
                if explicit is None or self._explicit_resolution_rank(bound) > self._explicit_resolution_rank(explicit):
                    return bound.session_id
            return claude_session_id
        if self._is_claude_state(bound):
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
        return self.get_or_create(
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
            self._persist(state)
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
        session_id = validate_session_id(session_id)
        state = self._states.get(session_id)
        resolved_claude_session_id = claude_session_id or (session_id if self._is_claude_session_id(session_id) else None)
        if state is not None:
            if user_id is not None:
                state.user_id = user_id
            state.provider = provider
            state.workdir = workdir
            if terminal_id is not None:
                state.terminal_id = terminal_id
            state.claude_session_id = resolved_claude_session_id or state.claude_session_id or state.session_id
            self._persist(state, publish=False)
            return state

        loaded = self._file_store.load_session_state(session_id)
        if loaded is not None:
            state = self._hydrate_and_cache_state(loaded)
            if user_id is not None:
                state.user_id = user_id
            state.provider = provider
            state.workdir = workdir
            if terminal_id is not None:
                state.terminal_id = terminal_id
            state.claude_session_id = resolved_claude_session_id or state.claude_session_id or state.session_id
        else:
            state = SessionState(
                session_id=session_id,
                user_id=user_id,
                provider=provider,
                workdir=workdir,
                terminal_id=terminal_id,
                claude_session_id=resolved_claude_session_id or session_id,
            )
            checkpoint = self._file_store.load_checkpoint(session_id)
            state.checkpoint = checkpoint
            state.turns = self._file_store.load_conversation(session_id)
            if state.turns:
                current = state.turns[-1]
                if not current.is_complete:
                    state.current_turn_id = current.turn_id
                    state.phase = SessionPhase.PROCESSING
                else:
                    state.last_reply = current.text.strip() or None
                    state.last_reply_role = current.role

        state.history_loaded = self._has_loaded_history(state)
        self._states[session_id] = state
        self._persist(state, publish=False)
        return state

    def get(self, session_id: str) -> SessionState | None:
        session_id = validate_session_id(session_id)
        state = self._states.get(session_id)
        if state is not None:
            return state
        loaded = self._file_store.load_session_state(session_id)
        if loaded is None:
            return None
        return self._hydrate_and_cache_state(loaded)

    def get_cursor(self, session_id: str) -> int:
        session_id = validate_session_id(session_id)
        state = self._states.get(session_id)
        if state is not None:
            return state.revision
        loaded = self._file_store.load_session_state(session_id)
        if loaded is None:
            return 0
        self._states[session_id] = loaded
        return loaded.revision

    def get_revision(self, session_id: str) -> int:
        return self.get_cursor(session_id)

    def get_structured_reply_cursor(self, session_id: str) -> tuple[str | None, str | None]:
        state = self.get(session_id)
        if state is None:
            return None, None
        return state.structured_reply_turn_id, state.structured_permission_key

    def get_structured_user_question_cursor(self, session_id: str) -> str | None:
        state = self.get(session_id)
        if state is None:
            return None
        return state.structured_user_question_key

    def mark_structured_reply_emitted(self, session_id: str, *, turn_id: str) -> SessionState:
        state = self.get(session_id)
        if state is None:
            state = self.get_or_create(session_id=session_id)
        if state.structured_reply_turn_id == turn_id:
            return state
        state.structured_reply_turn_id = turn_id
        self._persist(state, publish=False)
        return state

    def mark_structured_permission_emitted(self, session_id: str, *, permission_key: str) -> SessionState:
        state = self.get(session_id)
        if state is None:
            state = self.get_or_create(session_id=session_id)
        if state.structured_permission_key == permission_key:
            return state
        state.structured_permission_key = permission_key
        self._persist(state, publish=False)
        return state

    def mark_structured_user_question_emitted(self, session_id: str, *, question_key: str) -> SessionState:
        state = self.get(session_id)
        if state is None:
            state = self.get_or_create(session_id=session_id)
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
        self._persist(state, publish=False)
        return state

    async def wait_for_publish(self, session_id: str, *, since_cursor: int, timeout_sec: float) -> bool:
        if self.get_cursor(session_id) > since_cursor:
            return True
        condition = self._revision_conditions.setdefault(session_id, asyncio.Condition())
        async with condition:
            if self.get_cursor(session_id) > since_cursor:
                return True
            try:
                await asyncio.wait_for(condition.wait_for(lambda: self.get_cursor(session_id) > since_cursor), timeout=timeout_sec)
            except asyncio.TimeoutError:
                return False
        return True

    async def wait_for_change(self, session_id: str, *, since_revision: int, timeout_sec: float) -> bool:
        return await self.wait_for_publish(session_id, since_cursor=since_revision, timeout_sec=timeout_sec)

    def save_checkpoint(self, session_id: str, checkpoint: ParserCheckpoint) -> SessionState:
        session_id = validate_session_id(session_id)
        state = self._states[session_id]
        state.checkpoint = checkpoint
        self._persist(state, publish=False)
        return state

    def _persist(self, state: SessionState, *, publish: bool = True) -> None:
        if publish:
            state.revision += 1
        self._file_store.save_checkpoint(state.session_id, state.checkpoint)
        self._file_store.save_session_state(state)
        if publish:
            self._publish(state.session_id)

    def _publish(self, session_id: str) -> None:
        condition = self._revision_conditions.get(session_id)
        if condition is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _notify() -> None:
            async with condition:
                condition.notify_all()

        loop.create_task(_notify())

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
