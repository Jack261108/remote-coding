from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.user_question_models import extract_user_question_prompts
from app.domain.hook_models import HookEvent, validate_session_id
from app.domain.session_models import (
    ConversationTurn,
    ParserCheckpoint,
    PendingPermission,
    SessionEvent,
    SessionEventType,
    SessionPhase,
    SessionState,
    SubagentState,
    SubagentToolCall,
    ToolCallRecord,
    ToolStatus,
)


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


class SessionStore:
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

    def process(self, event: SessionEvent) -> SessionState:
        event.session_id = validate_session_id(event.session_id)
        state = self._states.get(event.session_id)
        if state is None:
            state = self.get_or_create(session_id=event.session_id)
        state.last_activity = event.at

        if event.type == SessionEventType.SESSION_STARTED:
            state.phase = SessionPhase.PROCESSING
            state.interrupted = False
        elif event.type == SessionEventType.TURN_STARTED:
            turn = ConversationTurn(turn_id=str(event.payload["turn_id"]), role=str(event.payload.get("role", "assistant")))
            state.turns.append(turn)
            state.current_turn_id = turn.turn_id
            state.phase = SessionPhase.PROCESSING
            state.interrupted = False
        elif event.type == SessionEventType.PARSER_UPDATED:
            turn = state.current_turn()
            if turn is not None:
                turn.text = str(event.payload.get("text", turn.text))
                turn.is_complete = bool(event.payload.get("is_complete", turn.is_complete))
                state.last_reply = turn.text.strip() or None
                state.last_reply_role = turn.role
                state.last_tool_name = None
        elif event.type == SessionEventType.TURN_COMPLETED:
            turn = state.current_turn()
            if turn is not None:
                turn.is_complete = True
                turn.ended_at = event.at
                state.last_reply = turn.text.strip() or None
                state.last_reply_role = turn.role
                state.last_tool_name = None
            state.phase = SessionPhase.WAITING_FOR_INPUT
            state.current_turn_id = None
        elif event.type == SessionEventType.SESSION_ENDED:
            state.phase = SessionPhase.ENDED
            state.pending_permission = None
        elif event.type == SessionEventType.HOOK_RECEIVED:
            self._process_hook_event(state, event)
        elif event.type in {SessionEventType.FILE_SYNCED, SessionEventType.HISTORY_LOADED}:
            self._process_file_synced(state, event)
        elif event.type == SessionEventType.CLEAR_DETECTED:
            self._clear_state(state)
        elif event.type == SessionEventType.INTERRUPT_DETECTED:
            self._interrupt_session_tools(state, event.at)
            state.interrupted = True
            self._move_to_next_phase(state, default=SessionPhase.WAITING_FOR_INPUT)
        elif event.type == SessionEventType.PERMISSION_APPROVED:
            self._process_permission_decision(state, event, approved=True)
        elif event.type == SessionEventType.PERMISSION_DENIED:
            self._process_permission_decision(state, event, approved=False)
        elif event.type == SessionEventType.PERMISSION_RESPONSE_FAILED:
            self._process_permission_response_failed(state, event)

        self._persist(state)
        return state

    def _process_hook_event(self, state: SessionState, event: SessionEvent) -> None:
        raw = event.payload.get("hook") if isinstance(event.payload.get("hook"), dict) else event.payload
        hook = HookEvent.from_dict(raw)
        state.workdir = hook.cwd or state.workdir
        state.claude_session_id = hook.session_id or state.claude_session_id or state.session_id
        state.interrupted = False

        if hook.event == "PreToolUse" and hook.tool_use_id:
            existing = state.tool_calls.get(hook.tool_use_id)
            state.tool_calls[hook.tool_use_id] = ToolCallRecord(
                tool_use_id=hook.tool_use_id,
                name=hook.tool or (existing.name if existing else "Tool"),
                input=hook.tool_input or (existing.input if existing else {}),
                status=existing.status if existing is not None else ToolStatus.RUNNING,
                result=existing.result if existing is not None else None,
                structured_result=existing.structured_result if existing is not None else None,
                subagent_tools=existing.subagent_tools if existing is not None else [],
                started_at=existing.started_at if existing is not None else event.at,
                completed_at=existing.completed_at if existing is not None else None,
            )
            if state.subagent_state.has_active_subagent and not state.tool_calls[hook.tool_use_id].is_subagent_container:
                current_task = state.subagent_state.current_task()
                if current_task is not None:
                    tool = SubagentToolCall(
                        tool_use_id=hook.tool_use_id,
                        name=hook.tool or "Tool",
                        input=hook.tool_input or {},
                        status=ToolStatus.RUNNING,
                        started_at=event.at,
                    )
                    state.subagent_state.add_subagent_tool(current_task.task_tool_id, tool)
                    state.tool_calls[current_task.task_tool_id].subagent_tools = current_task.subagent_tools
            elif state.tool_calls[hook.tool_use_id].is_subagent_container:
                description = hook.tool_input.get("description") if isinstance(hook.tool_input, dict) else None
                state.subagent_state.start_task(task_tool_id=hook.tool_use_id, description=str(description) if description is not None else None)
            state.phase = SessionPhase.PROCESSING
        elif hook.event == "PermissionRequest" and hook.tool_use_id:
            existing = state.tool_calls.get(hook.tool_use_id)
            state.tool_calls[hook.tool_use_id] = ToolCallRecord(
                tool_use_id=hook.tool_use_id,
                name=hook.tool or (existing.name if existing else "Tool"),
                input=hook.tool_input or (existing.input if existing else {}),
                status=ToolStatus.WAITING_FOR_APPROVAL,
                result=existing.result if existing is not None else None,
                structured_result=existing.structured_result if existing is not None else None,
                started_at=existing.started_at if existing is not None else event.at,
                completed_at=existing.completed_at if existing is not None else None,
            )
            state.pending_permission = PendingPermission(
                tool_use_id=hook.tool_use_id,
                tool_name=hook.tool or "Tool",
                tool_input=hook.tool_input,
                received_at=event.at,
            )
            state.phase = SessionPhase.WAITING_FOR_APPROVAL
        elif hook.event in {"PostToolUse", "PostToolUseFailure"} and hook.tool_use_id:
            existing = state.tool_calls.get(hook.tool_use_id)
            if existing is not None:
                existing.status = ToolStatus.SUCCESS if hook.event == "PostToolUse" else ToolStatus.ERROR
                existing.completed_at = event.at
            if state.pending_permission and state.pending_permission.tool_use_id == hook.tool_use_id:
                state.pending_permission = None
            if existing is not None and existing.is_subagent_container:
                state.subagent_state.stop_task(task_tool_id=hook.tool_use_id)
            elif state.subagent_state.has_active_subagent:
                current_task = state.subagent_state.current_task()
                if current_task is not None:
                    state.subagent_state.update_subagent_tool_status(current_task.task_tool_id, hook.tool_use_id, ToolStatus.SUCCESS if hook.event == "PostToolUse" else ToolStatus.ERROR)
                    container = state.tool_calls.get(current_task.task_tool_id)
                    if container is not None:
                        container.subagent_tools = current_task.subagent_tools
            self._move_to_next_phase(state, default=SessionPhase.PROCESSING)
        elif hook.event == "PreCompact":
            state.phase = SessionPhase.COMPACTING
        elif hook.event == "StopFailure":
            self._interrupt_session_tools(state, event.at)
            state.interrupted = True
            state.phase = SessionPhase.WAITING_FOR_INPUT
        elif hook.event == "SessionEnd" or hook.status == "ended":
            state.interrupted = self._interrupt_session_tools(state, event.at)
            state.phase = SessionPhase.ENDED
        elif hook.event in {"Stop", "SubagentStop"} or hook.status == "waiting_for_input":
            state.interrupted = self._interrupt_session_tools(state, event.at)
            state.phase = SessionPhase.WAITING_FOR_INPUT
        elif hook.status in {"running_tool", "processing", "starting"}:
            state.phase = SessionPhase.PROCESSING

    def _process_file_synced(self, state: SessionState, event: SessionEvent) -> None:
        payload = event.payload
        state.workdir = str(payload.get("cwd", state.workdir))
        state.claude_session_id = str(payload.get("claude_session_id") or state.claude_session_id or state.session_id)
        last_offset = int(payload["last_offset"]) if payload.get("last_offset") is not None else None
        reset_detected = bool(payload.get("reset_detected", False))
        turns_payload = payload.get("turns", [])
        parsed_turns = [
            item if isinstance(item, ConversationTurn) else ConversationTurn.from_dict(item)
            for item in turns_payload
        ]
        tool_calls_payload = payload.get("tool_calls", {})
        parsed_tool_calls: dict[str, ToolCallRecord] = {}
        if isinstance(tool_calls_payload, dict):
            for key, value in tool_calls_payload.items():
                parsed_tool_calls[str(key)] = value if isinstance(value, ToolCallRecord) else ToolCallRecord.from_dict(value)
        self._preserve_hook_only_runtime_state(state, parsed_tool_calls)

        if last_offset is not None and last_offset < state.checkpoint.last_offset and not reset_detected:
            has_newer_turns = len(parsed_turns) > len(state.turns)
            has_more_tool_calls = len(parsed_tool_calls) > len(state.tool_calls)
            if not has_newer_turns and not has_more_tool_calls:
                return

        if payload.get("clear_detected"):
            state.turns = parsed_turns
            state.tool_calls = parsed_tool_calls
            state.pending_permission = None
        elif payload.get("turns") is not None:
            state.turns = parsed_turns
            state.tool_calls = parsed_tool_calls

        for task_tool_id, container in state.tool_calls.items():
            if container.subagent_tools:
                state.subagent_state.populate_from_container(task_tool_id, container.subagent_tools)

        if state.subagent_state.has_active_subagent:
            for task_tool_id, task in state.subagent_state.active_tasks.items():
                container = state.tool_calls.get(task_tool_id)
                if container is not None and not container.subagent_tools:
                    container.subagent_tools = task.subagent_tools

        state.current_turn_id = None
        state.summary = str(payload["summary"]) if payload.get("summary") is not None else state.summary
        state.last_reply = str(payload["last_reply"]) if payload.get("last_reply") is not None else state.last_reply
        state.last_reply_role = str(payload["last_reply_role"]) if payload.get("last_reply_role") is not None else state.last_reply_role
        state.last_tool_name = str(payload["last_tool_name"]) if payload.get("last_tool_name") is not None else state.last_tool_name
        state.history_loaded = True
        state.clear_detected = bool(payload.get("clear_detected", False))
        state.interrupted = bool(payload.get("interrupt_detected", False))
        if last_offset is not None:
            state.checkpoint.last_offset = last_offset
        state.checkpoint.clear_pending = state.clear_detected
        state.checkpoint.last_summary = state.summary or ""
        state.checkpoint.seen_tool_ids = list(state.tool_calls.keys())
        state.checkpoint.completed_tool_ids = [
            tool_id
            for tool_id, tool in state.tool_calls.items()
            if tool.status in {ToolStatus.SUCCESS, ToolStatus.ERROR, ToolStatus.INTERRUPTED}
        ]
        state.checkpoint.tool_id_to_name = {tool_id: tool.name for tool_id, tool in state.tool_calls.items()}

        self._move_to_next_phase(state, default=SessionPhase.IDLE)

    def _preserve_hook_only_runtime_state(self, state: SessionState, parsed_tool_calls: dict[str, ToolCallRecord]) -> None:
        for tool_id, existing in state.tool_calls.items():
            if existing.status != ToolStatus.WAITING_FOR_APPROVAL:
                continue
            candidate = parsed_tool_calls.get(tool_id)
            if candidate is None:
                parsed_tool_calls[tool_id] = ToolCallRecord.from_dict(existing.to_dict())
                continue
            if candidate.status == ToolStatus.RUNNING:
                candidate.status = ToolStatus.WAITING_FOR_APPROVAL
                if not candidate.input and existing.input:
                    candidate.input = dict(existing.input)

    def _process_permission_decision(self, state: SessionState, event: SessionEvent, *, approved: bool) -> None:
        tool_use_id = str(event.payload.get("tool_use_id", ""))
        if not tool_use_id:
            return
        tool = state.tool_calls.get(tool_use_id)
        if tool is not None:
            tool.status = ToolStatus.RUNNING if approved else ToolStatus.ERROR
            if not approved:
                tool.completed_at = event.at
        if state.pending_permission and state.pending_permission.tool_use_id == tool_use_id:
            state.pending_permission = None
        state.interrupted = not approved
        self._move_to_next_phase(state, default=SessionPhase.PROCESSING if approved else SessionPhase.IDLE)

    def _process_permission_response_failed(self, state: SessionState, event: SessionEvent) -> None:
        tool_use_id = str(event.payload.get("tool_use_id", ""))
        if not tool_use_id:
            state.interrupted = self._interrupt_session_tools(state, event.at)
            self._move_to_next_phase(state, default=SessionPhase.WAITING_FOR_INPUT)
            return
        tool = state.tool_calls.get(tool_use_id)
        if tool is not None and tool.status in {ToolStatus.RUNNING, ToolStatus.WAITING_FOR_APPROVAL}:
            tool.status = ToolStatus.INTERRUPTED
            tool.completed_at = tool.completed_at or event.at
            state.interrupted = True
        if state.pending_permission and state.pending_permission.tool_use_id == tool_use_id:
            state.pending_permission = None
        self._move_to_next_phase(state, default=SessionPhase.WAITING_FOR_INPUT)

    def _move_to_next_phase(self, state: SessionState, *, default: SessionPhase) -> None:
        next_pending = None
        has_running_tool = False
        for tool in state.tool_calls.values():
            if tool.status == ToolStatus.WAITING_FOR_APPROVAL:
                next_pending = tool
                break
            if tool.status == ToolStatus.RUNNING:
                has_running_tool = True
        if next_pending is not None:
            state.pending_permission = PendingPermission(
                tool_use_id=next_pending.tool_use_id,
                tool_name=next_pending.name,
                tool_input=next_pending.input,
            )
            state.phase = SessionPhase.WAITING_FOR_APPROVAL
            return
        state.pending_permission = None
        if has_running_tool:
            state.phase = SessionPhase.PROCESSING
            return
        if default == SessionPhase.ENDED:
            state.phase = SessionPhase.ENDED
            return
        if state.turns and state.turns[-1].role == "assistant" and state.turns[-1].is_complete:
            state.phase = SessionPhase.WAITING_FOR_INPUT
            return
        state.phase = default

    def _clear_state(self, state: SessionState) -> None:
        state.turns = []
        state.tool_calls = {}
        state.pending_permission = None
        state.current_turn_id = None
        state.summary = None
        state.last_reply = None
        state.last_reply_role = None
        state.last_tool_name = None
        state.subagent_state = SubagentState()
        state.history_loaded = True
        state.clear_detected = True
        state.interrupted = False
        state.checkpoint.clear_pending = True
        state.checkpoint.seen_tool_ids = []
        state.checkpoint.completed_tool_ids = []
        state.checkpoint.tool_id_to_name = {}
        state.phase = SessionPhase.WAITING_FOR_INPUT

    def _interrupt_session_tools(self, state: SessionState, at) -> bool:
        interrupted = False
        for tool in state.tool_calls.values():
            if tool.status in {ToolStatus.RUNNING, ToolStatus.WAITING_FOR_APPROVAL}:
                tool.status = ToolStatus.INTERRUPTED
                tool.completed_at = tool.completed_at or at
                interrupted = True
        state.pending_permission = None
        return interrupted

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
