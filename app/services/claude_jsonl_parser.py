from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.adapters.claude.paths import ClaudePaths
from app.domain.hook_models import validate_path_component, validate_session_id
from app.domain.models import utc_now
from app.domain.session_models import ConversationTurn, SubagentToolCall, ToolCallRecord, ToolStatus

logger = logging.getLogger(__name__)

_SUBAGENT_TOOL_RESULT_KEYS = {"agentId", "status", "content", "prompt", "totalDurationMs", "totalTokens", "totalToolUseCount"}
_INTERRUPT_PATTERNS = (
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
    "Interrupted by user",
    "interrupted by user",
    "user doesn't want to proceed",
)


@dataclass
class ClaudeJSONLSnapshot:
    session_id: str
    cwd: str
    turns: list[ConversationTurn]
    tool_calls: dict[str, ToolCallRecord]
    summary: str | None = None
    last_reply: str | None = None
    last_reply_role: str | None = None
    last_tool_name: str | None = None
    clear_detected: bool = False
    interrupt_detected: bool = False
    reset_detected: bool = False
    last_offset: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "turns": [turn.to_dict() for turn in self.turns],
            "tool_calls": {key: value.to_dict() for key, value in self.tool_calls.items()},
            "summary": self.summary,
            "last_reply": self.last_reply,
            "last_reply_role": self.last_reply_role,
            "last_tool_name": self.last_tool_name,
            "claude_session_id": self.session_id,
            "clear_detected": self.clear_detected,
            "interrupt_detected": self.interrupt_detected,
            "reset_detected": self.reset_detected,
            "last_offset": self.last_offset,
        }


@dataclass
class _ParserState:
    last_offset: int = 0
    turns: list[ConversationTurn] = field(default_factory=list)
    tool_calls: dict[str, ToolCallRecord] = field(default_factory=dict)
    summary: str | None = None
    last_reply: str | None = None
    last_reply_role: str | None = None
    last_tool_name: str | None = None
    clear_detected: bool = False
    interrupt_detected: bool = False


class ClaudeJSONLParser:
    def __init__(self, paths: ClaudePaths) -> None:
        self._paths = paths
        self._states: dict[str, _ParserState] = {}

    def parse_incremental(self, *, session_id: str, cwd: str) -> ClaudeJSONLSnapshot:
        session_file = self.session_file_path(session_id=session_id, cwd=cwd)
        state = self._states.get(session_id) or _ParserState()
        state.clear_detected = False
        state.interrupt_detected = False
        reset_detected = False

        if not session_file.exists():
            if state.last_offset:
                state = _ParserState()
                reset_detected = True
            snapshot = self._snapshot(session_id=session_id, cwd=cwd, state=state, reset_detected=reset_detected)
            self._states[session_id] = state
            return snapshot

        file_size = session_file.stat().st_size
        if file_size < state.last_offset:
            state = _ParserState()
            reset_detected = True

        if file_size > state.last_offset:
            with session_file.open("rb") as fh:
                fh.seek(state.last_offset)
                data = fh.read()
            consumed = 0
            for raw_line in data.splitlines(keepends=True):
                if not raw_line.endswith((b"\n", b"\r")):
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    consumed += len(raw_line)
                    continue
                try:
                    self._process_line(line, state, session_id=session_id, cwd=cwd)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSONL line at offset %d", state.last_offset + consumed)
                    consumed += len(raw_line)
                    continue
                consumed += len(raw_line)
            state.last_offset += consumed

        self._states[session_id] = state
        return self._snapshot(session_id=session_id, cwd=cwd, state=state, reset_detected=reset_detected)

    def reset_state(self, session_id: str) -> None:
        self._states.pop(session_id, None)

    def session_file_path(self, *, session_id: str, cwd: str) -> Path:
        safe_session_id = validate_session_id(session_id)
        project_dir = cwd.replace("/", "-").replace(".", "-")
        return self._paths.projects_dir / project_dir / f"{safe_session_id}.jsonl"

    def extract_session_title(self, *, session_id: str, cwd: str, max_length: int = 60) -> str | None:
        """Extract the first user message text as session title.

        Skips isMeta messages and system-injected caveats (content starting with '<').
        Uses only the first line to match Claude CLI terminal title behavior.
        Returns None on any failure without raising.
        """
        try:
            path = self.session_file_path(session_id=session_id, cwd=cwd)
            if not path.exists():
                return None
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    if record.get("type") != "user":
                        continue
                    if record.get("isMeta"):
                        continue
                    msg = record.get("message", {})
                    content = msg.get("content", "")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                break
                    text = text.split("\n", 1)[0].strip()
                    if not text or text.startswith("<"):
                        continue
                    if len(text) > max_length:
                        return text[:max_length] + "…"
                    return text
            return None
        except Exception:
            return None

    def subagent_file_path(self, *, session_id: str, agent_id: str, cwd: str) -> Path:
        safe_session_id = validate_session_id(session_id)
        safe_agent_id = validate_path_component(agent_id, field_name="agent_id")
        project_dir = cwd.replace("/", "-").replace(".", "-")
        nested = self._paths.projects_dir / project_dir / safe_session_id / "subagents" / f"agent-{safe_agent_id}.jsonl"
        flat = self._paths.projects_dir / project_dir / f"agent-{safe_agent_id}.jsonl"
        return nested if nested.exists() else flat

    def parse_subagent_tools(self, *, session_id: str, agent_id: str, cwd: str) -> list[SubagentToolCall]:
        if not agent_id:
            return []
        agent_file = self.subagent_file_path(session_id=session_id, agent_id=agent_id, cwd=cwd)
        if not agent_file.exists():
            return []

        tool_order: list[str] = []
        tool_uses: dict[str, tuple[str, dict[str, Any], datetime]] = {}
        completed_tool_ids: set[str] = set()
        error_tool_ids: set[str] = set()
        results_by_tool_id: dict[str, tuple[str | None, dict[str, Any] | None]] = {}

        with agent_file.open(encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or ('"tool_use"' not in line and '"tool_result"' not in line):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = payload.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                timestamp = self._parse_timestamp(payload.get("timestamp"))
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "tool_result":
                        tool_use_id = str(block.get("tool_use_id") or "")
                        if not tool_use_id:
                            continue
                        completed_tool_ids.add(tool_use_id)
                        if block.get("is_error"):
                            error_tool_ids.add(tool_use_id)
                        results_by_tool_id[tool_use_id] = (
                            self._tool_result_text(block, payload),
                            self._tool_result_payload(block, payload),
                        )
                    elif block_type == "tool_use":
                        tool_id = str(block.get("id") or "")
                        if not tool_id or tool_id in tool_uses:
                            continue
                        tool_order.append(tool_id)
                        tool_uses[tool_id] = (
                            str(block.get("name") or "Tool"),
                            dict(block.get("input", {})) if isinstance(block.get("input"), dict) else {},
                            timestamp,
                        )

        tools: list[SubagentToolCall] = []
        for tool_id in tool_order:
            name, input_payload, timestamp = tool_uses[tool_id]
            result_text, structured_result = results_by_tool_id.get(tool_id, (None, None))
            if tool_id in error_tool_ids:
                status = ToolStatus.ERROR
            elif tool_id in completed_tool_ids:
                status = ToolStatus.SUCCESS
            else:
                status = ToolStatus.RUNNING
            tools.append(
                SubagentToolCall(
                    tool_use_id=tool_id,
                    name=name,
                    input=input_payload,
                    status=status,
                    result=result_text,
                    structured_result=structured_result,
                    started_at=timestamp,
                    completed_at=timestamp if tool_id in completed_tool_ids else None,
                )
            )

        return tools

    def _process_line(self, line: str, state: _ParserState, *, session_id: str, cwd: str) -> None:
        payload = json.loads(line)
        message_type = str(payload.get("type", ""))

        if message_type == "summary":
            summary = payload.get("summary")
            state.summary = str(summary) if summary is not None else None
            return

        if self._is_clear_command(payload):
            state.turns = []
            state.tool_calls = {}
            state.summary = None
            state.last_reply = None
            state.last_reply_role = None
            state.last_tool_name = None
            state.clear_detected = True
            return

        if self._is_interrupt_payload(payload):
            state.interrupt_detected = True

        if message_type not in {"user", "assistant"}:
            return

        payload.setdefault("sessionId", session_id)
        payload.setdefault("session_id", session_id)
        payload.setdefault("cwd", cwd)
        if self._process_subagent_payload(payload, state):
            return

        if payload.get("isMeta"):
            return

        message = payload.get("message")
        if not isinstance(message, dict):
            return

        timestamp = self._parse_timestamp(payload.get("timestamp"))
        content = message.get("content")
        if isinstance(content, str):
            text = self._normalize_text(content)
            if not text:
                return
            role = "assistant" if message_type == "assistant" else "user"
            state.turns.append(
                ConversationTurn(
                    turn_id=str(message.get("id") or f"{message_type}-{len(state.turns)}"),
                    role=role,
                    text=text,
                    source="jsonl",
                    is_complete=True,
                    started_at=timestamp,
                    ended_at=timestamp,
                )
            )
            state.last_reply = text.strip()
            state.last_reply_role = role
            if role != "tool":
                state.last_tool_name = None
            return

        if not isinstance(content, list):
            return

        assistant_texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", ""))
            if block_type == "text":
                text = self._normalize_text(block.get("text"))
                if text:
                    assistant_texts.append(text.strip())
            elif block_type == "tool_use":
                tool_use_id = str(block.get("id") or "")
                if not tool_use_id:
                    continue
                state.tool_calls[tool_use_id] = ToolCallRecord(
                    tool_use_id=tool_use_id,
                    name=str(block.get("name") or "Tool"),
                    input=dict(block.get("input", {})) if isinstance(block.get("input"), dict) else {},
                    status=state.tool_calls.get(
                        tool_use_id, ToolCallRecord(tool_use_id=tool_use_id, name=str(block.get("name") or "Tool"))
                    ).status,
                    result=state.tool_calls.get(tool_use_id).result if tool_use_id in state.tool_calls else None,  # type: ignore[union-attr]
                    structured_result=state.tool_calls.get(tool_use_id).structured_result if tool_use_id in state.tool_calls else None,  # type: ignore[union-attr]
                    started_at=state.tool_calls.get(tool_use_id).started_at if tool_use_id in state.tool_calls else timestamp,  # type: ignore[union-attr]
                    completed_at=state.tool_calls.get(tool_use_id).completed_at if tool_use_id in state.tool_calls else None,  # type: ignore[union-attr]
                )
                state.last_reply_role = "tool"
                state.last_tool_name = str(block.get("name") or "Tool")
                state.last_reply = self._tool_preview(str(block.get("name") or "Tool"), block.get("input"))
            elif block_type == "tool_result":
                tool_use_id = str(block.get("tool_use_id") or "")
                if not tool_use_id:
                    continue
                existing = state.tool_calls.get(tool_use_id)
                tool_name = existing.name if existing is not None else "Tool"
                status = ToolStatus.ERROR if bool(block.get("is_error", False)) else ToolStatus.SUCCESS
                if status == ToolStatus.ERROR and self._is_interrupt_result(block, payload):
                    status = ToolStatus.INTERRUPTED
                    state.interrupt_detected = True
                state.tool_calls[tool_use_id] = ToolCallRecord(
                    tool_use_id=tool_use_id,
                    name=tool_name,
                    input=existing.input if existing is not None else {},
                    status=status,
                    result=self._tool_result_text(block, payload),
                    structured_result=self._tool_result_payload(block, payload),
                    started_at=existing.started_at if existing is not None else timestamp,
                    completed_at=timestamp,
                )

        if assistant_texts and message_type == "assistant":
            text = self._normalize_text("\n\n".join(assistant_texts))
            if text:
                state.turns.append(
                    ConversationTurn(
                        turn_id=str(message.get("id") or f"assistant-{len(state.turns)}"),
                        role="assistant",
                        text=text,
                        source="jsonl",
                        is_complete=True,
                        started_at=timestamp,
                        ended_at=timestamp,
                    )
                )
                state.last_reply = text.strip()
                state.last_reply_role = "assistant"
                state.last_tool_name = None

    def _snapshot(self, *, session_id: str, cwd: str, state: _ParserState, reset_detected: bool) -> ClaudeJSONLSnapshot:
        return ClaudeJSONLSnapshot(
            session_id=session_id,
            cwd=cwd,
            turns=[ConversationTurn.from_dict(turn.to_dict()) for turn in state.turns],
            tool_calls={key: ToolCallRecord.from_dict(value.to_dict()) for key, value in state.tool_calls.items()},
            summary=state.summary,
            last_reply=state.last_reply,
            last_reply_role=state.last_reply_role,
            last_tool_name=state.last_tool_name,
            clear_detected=state.clear_detected,
            interrupt_detected=state.interrupt_detected or any(tool.status == ToolStatus.INTERRUPTED for tool in state.tool_calls.values()),
            reset_detected=reset_detected,
            last_offset=state.last_offset,
        )

    def _process_subagent_payload(self, payload: dict[str, Any], state: _ParserState) -> bool:
        tool_result = payload.get("toolUseResult")
        if not isinstance(tool_result, dict):
            return False
        if not _SUBAGENT_TOOL_RESULT_KEYS.intersection(tool_result.keys()):
            return False

        message = payload.get("message")
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if not isinstance(content, list):
            return False

        timestamp = self._parse_timestamp(payload.get("timestamp"))
        updated = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id") or "")
            if not tool_use_id:
                continue
            existing = state.tool_calls.get(tool_use_id)
            if existing is None:
                tool_use_block = next(
                    (
                        item
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "tool_use" and str(item.get("id") or "") == tool_use_id
                    ),
                    None,
                )
                if tool_use_block is None:
                    continue
                existing = ToolCallRecord(
                    tool_use_id=tool_use_id,
                    name=str(tool_use_block.get("name") or "Tool"),
                    input=dict(tool_use_block.get("input", {})) if isinstance(tool_use_block.get("input"), dict) else {},
                    started_at=timestamp,
                )
                state.tool_calls[tool_use_id] = existing
            existing.status = ToolStatus.ERROR if bool(block.get("is_error", False)) else ToolStatus.SUCCESS
            existing.result = self._tool_result_text(block, payload)
            existing.structured_result = self._tool_result_payload(block, payload)
            existing.completed_at = timestamp
            agent_id = ""
            if isinstance(existing.structured_result, dict):
                agent_id = str(existing.structured_result.get("agentId") or "")
            if existing.is_subagent_container and agent_id:
                existing.subagent_tools = self.parse_subagent_tools(
                    session_id=payload.get("sessionId") or payload.get("session_id") or "",
                    agent_id=agent_id,
                    cwd=payload.get("cwd") or "",
                )
            updated = True
        return updated

    def _is_clear_command(self, payload: dict[str, Any]) -> bool:
        if payload.get("type") != "user" or payload.get("isMeta"):
            return False
        message = payload.get("message")
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if isinstance(content, str):
            return "<command-name>/clear</command-name>" in content
        return False

    def _is_interrupt_payload(self, payload: dict[str, Any]) -> bool:
        message = payload.get("message")
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if isinstance(content, str):
            return any(pattern in content for pattern in _INTERRUPT_PATTERNS)
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and any(pattern in str(block.get("text") or "") for pattern in _INTERRUPT_PATTERNS):
                return True
            if block.get("type") == "tool_result" and self._is_interrupt_result(block, payload):
                return True
        return False

    def _is_interrupt_result(self, block: dict[str, Any], payload: dict[str, Any]) -> bool:
        texts: list[str] = []
        content = block.get("content")
        if isinstance(content, str):
            texts.append(content)
        result = payload.get("toolUseResult")
        if isinstance(result, dict):
            for key in ("stdout", "stderr", "output", "content"):
                value = result.get(key)
                if isinstance(value, str):
                    texts.append(value)
            interrupted = result.get("interrupted")
            if interrupted is True:
                return True
        return any(pattern in text for text in texts for pattern in _INTERRUPT_PATTERNS)

    def _parse_timestamp(self, value: Any) -> datetime:
        if not value:
            return utc_now()
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return utc_now()

    def _normalize_text(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return f"\n{text}\n"

    def _tool_preview(self, tool_name: str, tool_input: Any) -> str:
        if isinstance(tool_input, dict):
            for key in ("command", "file_path", "pattern", "description", "query"):
                value = tool_input.get(key)
                if isinstance(value, str) and value.strip():
                    return f"{tool_name}: {value.strip()}"
        return tool_name

    def _tool_result_text(self, block: dict[str, Any], payload: dict[str, Any]) -> str | None:
        content = block.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        result = payload.get("toolUseResult")
        if isinstance(result, dict):
            for key in ("stdout", "stderr", "output"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _tool_result_payload(self, block: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(block.get("content"), list):
            return {"content": block["content"]}
        result = payload.get("toolUseResult")
        if isinstance(result, dict):
            return dict(result)
        return None
