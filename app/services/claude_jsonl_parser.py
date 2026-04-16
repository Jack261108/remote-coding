from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.adapters.claude.paths import ClaudePaths
from app.domain.models import utc_now
from app.domain.session_models import ConversationTurn, ToolCallRecord, ToolStatus


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
            "clear_detected": self.clear_detected,
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


class ClaudeJSONLParser:
    def __init__(self, paths: ClaudePaths) -> None:
        self._paths = paths
        self._states: dict[str, _ParserState] = {}

    def parse_incremental(self, *, session_id: str, cwd: str) -> ClaudeJSONLSnapshot:
        session_file = self.session_file_path(session_id=session_id, cwd=cwd)
        state = self._states.get(session_id) or _ParserState()
        state.clear_detected = False
        reset_detected = False

        if not session_file.exists():
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
                    self._process_line(line, state)
                except json.JSONDecodeError:
                    break
                consumed += len(raw_line)
            state.last_offset += consumed

        self._states[session_id] = state
        return self._snapshot(session_id=session_id, cwd=cwd, state=state, reset_detected=reset_detected)

    def reset_state(self, session_id: str) -> None:
        self._states.pop(session_id, None)

    def session_file_path(self, *, session_id: str, cwd: str) -> Path:
        project_dir = cwd.replace("/", "-").replace(".", "-")
        return self._paths.projects_dir / project_dir / f"{session_id}.jsonl"

    def _process_line(self, line: str, state: _ParserState) -> None:
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

        if message_type not in {"user", "assistant"}:
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
        for index, block in enumerate(content):
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
                    status=state.tool_calls.get(tool_use_id, ToolCallRecord(tool_use_id=tool_use_id, name=str(block.get("name") or "Tool"))).status,
                    result=state.tool_calls.get(tool_use_id).result if tool_use_id in state.tool_calls else None,
                    structured_result=state.tool_calls.get(tool_use_id).structured_result if tool_use_id in state.tool_calls else None,
                    started_at=state.tool_calls.get(tool_use_id).started_at if tool_use_id in state.tool_calls else timestamp,
                    completed_at=state.tool_calls.get(tool_use_id).completed_at if tool_use_id in state.tool_calls else None,
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
            reset_detected=reset_detected,
            last_offset=state.last_offset,
        )

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
