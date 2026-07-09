from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.bot.presenters.structured_reply_models import _StructuredSnapshot, _SubagentToolStateSnapshot, _ToolStateSnapshot
from app.bot.presenters.structured_reply_text import normalize_stream_text, preview_stream_text

if TYPE_CHECKING:
    from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


class StructuredReplySnapshotLoader:
    def __init__(self, *, task_service: TaskService, user_id: int, task_id: str | None = None) -> None:
        self._task_service = task_service
        self._user_id = user_id
        self._task_id = task_id

    async def load_session(self, *, log_missing: bool):
        if self._task_id is not None:
            return await self._task_service.get_structured_session_for_task(
                task_id=self._task_id,
                user_id=self._user_id,
                log_missing=log_missing,
            )
        return await self._task_service.get_structured_session(self._user_id, log_missing=log_missing)

    async def load_snapshot(self, *, log_missing: bool) -> _StructuredSnapshot:
        session = await self.load_session(log_missing=log_missing)
        if session is None:
            if log_missing:
                logger.info("structured reply unavailable", extra={"user_id": self._user_id, "reason": "no_structured_session"})
            return _StructuredSnapshot(session_id=None, turn_id=None, reply="", session_available=False)

        phase = session.phase.value
        session_user_id = session.user_id if getattr(session, "user_id", None) is not None else self._user_id
        session_cwd = getattr(session, "workdir", None)
        session_title = getattr(session, "title", None)
        tool_states = tuple(self._collect_tool_states(session))
        pending = getattr(session, "pending_permission", None)
        pending_permission_key = None
        pending_permission_tool_use_id = None
        pending_permission_tool_name = None
        pending_permission_tool_input = None
        if pending is not None:
            pending_permission_key = f"{pending.tool_use_id}:{pending.tool_name}"
            pending_permission_tool_use_id = pending.tool_use_id
            pending_permission_tool_name = pending.tool_name
            pending_permission_tool_input = pending.tool_input

        if not session.turns:
            logger.info(
                "structured reply unavailable",
                extra={"user_id": self._user_id, "reason": "no_turns", "phase": phase},
            )
            return _StructuredSnapshot(
                session_id=session.session_id,
                turn_id=None,
                reply="",
                session_available=True,
                phase=phase,
                pending_permission_key=pending_permission_key,
                pending_permission_tool_use_id=pending_permission_tool_use_id,
                pending_permission_tool_name=pending_permission_tool_name,
                pending_permission_tool_input=pending_permission_tool_input,
                cwd=session_cwd,
                session_title=session_title,
                user_id=session_user_id,
                tool_states=tool_states,
            )

        for turn in reversed(session.turns):
            if turn.role != "assistant" or not turn.is_complete:
                continue
            normalized_reply = normalize_stream_text(turn.text)
            if not normalized_reply:
                continue
            preview = preview_stream_text(normalized_reply)
            logger.info(
                "structured reply loaded",
                extra={
                    "user_id": self._user_id,
                    "turn_id": turn.turn_id,
                    "phase": phase,
                    "turn_count": len(session.turns),
                    "preview_len": len(preview),
                },
            )
            return _StructuredSnapshot(
                session_id=session.session_id,
                turn_id=turn.turn_id,
                reply=normalized_reply,
                session_available=True,
                phase=phase,
                pending_permission_key=pending_permission_key,
                pending_permission_tool_use_id=pending_permission_tool_use_id,
                pending_permission_tool_name=pending_permission_tool_name,
                pending_permission_tool_input=pending_permission_tool_input,
                cwd=session_cwd,
                session_title=session_title,
                user_id=session_user_id,
                tool_states=tool_states,
                turn_started_at=turn.started_at,
                turn_ended_at=turn.ended_at,
            )

        logger.info(
            "structured reply unavailable",
            extra={
                "user_id": self._user_id,
                "reason": "no_completed_assistant_turn",
                "phase": phase,
                "turn_count": len(session.turns),
            },
        )
        return _StructuredSnapshot(
            session_id=session.session_id,
            turn_id=None,
            reply="",
            session_available=True,
            phase=phase,
            pending_permission_key=pending_permission_key,
            pending_permission_tool_use_id=pending_permission_tool_use_id,
            pending_permission_tool_name=pending_permission_tool_name,
            pending_permission_tool_input=pending_permission_tool_input,
            cwd=session_cwd,
            session_title=session_title,
            user_id=session_user_id,
            tool_states=tool_states,
        )

    def _collect_tool_states(self, session) -> list[_ToolStateSnapshot]:
        tool_calls = getattr(session, "tool_calls", {}) or {}
        if not isinstance(tool_calls, dict):
            return []

        states: list[_ToolStateSnapshot] = []
        for tool_use_id, tool in tool_calls.items():
            status = getattr(tool, "status", None)
            status_value = getattr(status, "value", status)
            tool_name = getattr(tool, "name", None)
            tool_input = getattr(tool, "input", None)
            if tool_input is not None and not isinstance(tool_input, dict):
                tool_input = None
            structured_result = getattr(tool, "structured_result", None)
            if structured_result is not None and not isinstance(structured_result, dict):
                structured_result = None
            result = getattr(tool, "result", None)
            states.append(
                _ToolStateSnapshot(
                    tool_use_id=str(tool_use_id),
                    tool_name=str(tool_name) if tool_name is not None else None,
                    tool_input=tool_input,
                    status=str(status_value) if status_value is not None else None,
                    result=str(result) if result is not None else None,
                    structured_result=structured_result,
                    subagent_tools=tuple(self._collect_subagent_tool_states(tool)),
                )
            )
        return states

    def _collect_subagent_tool_states(self, tool) -> list[_SubagentToolStateSnapshot]:
        subagent_tools = getattr(tool, "subagent_tools", ()) or ()
        states: list[_SubagentToolStateSnapshot] = []
        for subagent_tool in subagent_tools:
            status = getattr(subagent_tool, "status", None)
            status_value = getattr(status, "value", status)
            tool_name = getattr(subagent_tool, "name", None)
            tool_input = getattr(subagent_tool, "input", None)
            if tool_input is not None and not isinstance(tool_input, dict):
                tool_input = None
            states.append(
                _SubagentToolStateSnapshot(
                    tool_use_id=str(getattr(subagent_tool, "tool_use_id", "")),
                    tool_name=str(tool_name) if tool_name is not None else None,
                    tool_input=tool_input,
                    status=str(status_value) if status_value is not None else None,
                )
            )
        return states
