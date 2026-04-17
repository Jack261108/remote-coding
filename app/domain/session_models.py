from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.domain.models import utc_now


class SessionPhase(str, Enum):
    IDLE = "idle"
    PROCESSING = "processing"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPACTING = "compacting"
    ENDED = "ended"


class SessionEventType(str, Enum):
    SESSION_STARTED = "session_started"
    RAW_CHUNK_APPENDED = "raw_chunk_appended"
    PARSER_UPDATED = "parser_updated"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    SESSION_ENDED = "session_ended"
    HOOK_RECEIVED = "hook_received"
    FILE_SYNCED = "file_synced"
    HISTORY_LOADED = "history_loaded"
    CLEAR_DETECTED = "clear_detected"
    INTERRUPT_DETECTED = "interrupt_detected"
    PERMISSION_APPROVED = "permission_approved"
    PERMISSION_DENIED = "permission_denied"
    PERMISSION_RESPONSE_FAILED = "permission_response_failed"


class ToolStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    WAITING_FOR_APPROVAL = "waiting_for_approval"


@dataclass
class ParserCheckpoint:
    last_offset: int = 0
    pending_buffer: str = ""
    in_reply_block: bool = False
    current_turn_id: str | None = None
    last_marker: str = ""
    last_emitted_fingerprint: str = ""
    seen_tool_ids: list[str] = field(default_factory=list)
    completed_tool_ids: list[str] = field(default_factory=list)
    tool_id_to_name: dict[str, str] = field(default_factory=dict)
    clear_pending: bool = False
    last_summary: str = ""
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_offset": self.last_offset,
            "pending_buffer": self.pending_buffer,
            "in_reply_block": self.in_reply_block,
            "current_turn_id": self.current_turn_id,
            "last_marker": self.last_marker,
            "last_emitted_fingerprint": self.last_emitted_fingerprint,
            "seen_tool_ids": self.seen_tool_ids,
            "completed_tool_ids": self.completed_tool_ids,
            "tool_id_to_name": self.tool_id_to_name,
            "clear_pending": self.clear_pending,
            "last_summary": self.last_summary,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ParserCheckpoint":
        if not data:
            return cls()
        updated_at = data.get("updated_at")
        return cls(
            last_offset=int(data.get("last_offset", 0)),
            pending_buffer=str(data.get("pending_buffer", "")),
            in_reply_block=bool(data.get("in_reply_block", False)),
            current_turn_id=data.get("current_turn_id"),
            last_marker=str(data.get("last_marker", "")),
            last_emitted_fingerprint=str(data.get("last_emitted_fingerprint", "")),
            seen_tool_ids=[str(item) for item in data.get("seen_tool_ids", [])],
            completed_tool_ids=[str(item) for item in data.get("completed_tool_ids", [])],
            tool_id_to_name={str(key): str(value) for key, value in dict(data.get("tool_id_to_name", {})).items()},
            clear_pending=bool(data.get("clear_pending", False)),
            last_summary=str(data.get("last_summary", "")),
            updated_at=datetime.fromisoformat(updated_at) if updated_at else utc_now(),
        )


@dataclass
class ConversationTurn:
    turn_id: str
    role: str
    text: str = ""
    source: str = "tmux"
    is_complete: bool = False
    started_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "role": self.role,
            "text": self.text,
            "source": self.source,
            "is_complete": self.is_complete,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationTurn":
        ended_at = data.get("ended_at")
        return cls(
            turn_id=str(data["turn_id"]),
            role=str(data["role"]),
            text=str(data.get("text", "")),
            source=str(data.get("source", "tmux")),
            is_complete=bool(data.get("is_complete", False)),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else utc_now(),
            ended_at=datetime.fromisoformat(ended_at) if ended_at else None,
        )


@dataclass
class SubagentToolCall:
    tool_use_id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    status: ToolStatus = ToolStatus.RUNNING
    result: str | None = None
    structured_result: dict[str, Any] | None = None
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "input": self.input,
            "status": self.status.value,
            "result": self.result,
            "structured_result": self.structured_result,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubagentToolCall":
        completed_at = data.get("completed_at")
        return cls(
            tool_use_id=str(data["tool_use_id"]),
            name=str(data.get("name", "")),
            input=dict(data.get("input", {})),
            status=ToolStatus(data.get("status", ToolStatus.RUNNING.value)),
            result=str(data["result"]) if data.get("result") is not None else None,
            structured_result=dict(data.get("structured_result", {})) if data.get("structured_result") is not None else None,
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else utc_now(),
            completed_at=datetime.fromisoformat(completed_at) if completed_at else None,
        )


@dataclass
class ToolCallRecord:
    tool_use_id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    status: ToolStatus = ToolStatus.RUNNING
    result: str | None = None
    structured_result: dict[str, Any] | None = None
    subagent_tools: list[SubagentToolCall] = field(default_factory=list)
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None

    @property
    def is_subagent_container(self) -> bool:
        return self.name in {"Task", "Agent"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "input": self.input,
            "status": self.status.value,
            "result": self.result,
            "structured_result": self.structured_result,
            "subagent_tools": [tool.to_dict() for tool in self.subagent_tools],
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCallRecord":
        completed_at = data.get("completed_at")
        return cls(
            tool_use_id=str(data["tool_use_id"]),
            name=str(data.get("name", "")),
            input=dict(data.get("input", {})),
            status=ToolStatus(data.get("status", ToolStatus.RUNNING.value)),
            result=str(data["result"]) if data.get("result") is not None else None,
            structured_result=dict(data.get("structured_result", {})) if data.get("structured_result") is not None else None,
            subagent_tools=[SubagentToolCall.from_dict(item) for item in data.get("subagent_tools", [])],
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else utc_now(),
            completed_at=datetime.fromisoformat(completed_at) if completed_at else None,
        )


@dataclass
class SubagentTaskState:
    task_tool_id: str
    description: str | None = None
    subagent_tools: list[SubagentToolCall] = field(default_factory=list)
    is_active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_tool_id": self.task_tool_id,
            "description": self.description,
            "subagent_tools": [tool.to_dict() for tool in self.subagent_tools],
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubagentTaskState":
        return cls(
            task_tool_id=str(data["task_tool_id"]),
            description=str(data["description"]) if data.get("description") is not None else None,
            subagent_tools=[SubagentToolCall.from_dict(item) for item in data.get("subagent_tools", [])],
            is_active=bool(data.get("is_active", True)),
        )


@dataclass
class SubagentState:
    active_tasks: dict[str, SubagentTaskState] = field(default_factory=dict)

    @property
    def has_active_subagent(self) -> bool:
        return any(task.is_active for task in self.active_tasks.values())

    def start_task(self, *, task_tool_id: str, description: str | None = None) -> None:
        self.active_tasks[task_tool_id] = SubagentTaskState(task_tool_id=task_tool_id, description=description)

    def stop_task(self, *, task_tool_id: str) -> None:
        task = self.active_tasks.get(task_tool_id)
        if task is not None:
            task.is_active = False
            self.active_tasks.pop(task_tool_id, None)

    def current_task(self) -> SubagentTaskState | None:
        if not self.active_tasks:
            return None
        task_tool_id = next(reversed(self.active_tasks))
        return self.active_tasks[task_tool_id]

    def add_subagent_tool(self, task_tool_id: str, tool: SubagentToolCall) -> None:
        self.active_tasks.setdefault(task_tool_id, SubagentTaskState(task_tool_id=task_tool_id)).subagent_tools.append(tool)

    def update_subagent_tool_status(self, task_tool_id: str, tool_id: str, status: ToolStatus) -> None:
        task = self.active_tasks.get(task_tool_id)
        if task is None:
            return
        for tool in task.subagent_tools:
            if tool.tool_use_id == tool_id:
                tool.status = status
                return

    def populate_from_container(self, task_tool_id: str, tools: list[SubagentToolCall]) -> None:
        task = self.active_tasks.setdefault(task_tool_id, SubagentTaskState(task_tool_id=task_tool_id))
        if tools:
            task.subagent_tools = tools

    def to_dict(self) -> dict[str, Any]:
        return {"active_tasks": {key: value.to_dict() for key, value in self.active_tasks.items()}}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SubagentState":
        if not data:
            return cls()
        active_tasks = data.get("active_tasks", {})
        return cls(
            active_tasks={str(key): SubagentTaskState.from_dict(value) for key, value in dict(active_tasks).items()},
        )


@dataclass
class PendingPermission:
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any] | None = None
    received_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "received_at": self.received_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PendingPermission | None":
        if not data:
            return None
        received_at = data.get("received_at")
        return cls(
            tool_use_id=str(data.get("tool_use_id", "")),
            tool_name=str(data.get("tool_name", "")),
            tool_input=dict(data.get("tool_input", {})) if data.get("tool_input") is not None else None,
            received_at=datetime.fromisoformat(received_at) if received_at else utc_now(),
        )


@dataclass
class SessionEvent:
    session_id: str
    type: SessionEventType
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "type": self.type.value,
            "payload": self.payload,
            "at": self.at.isoformat(),
        }


@dataclass
class SessionState:
    session_id: str
    user_id: int | None = None
    provider: str = "claude_code"
    workdir: str = "."
    terminal_id: str | None = None
    claude_session_id: str | None = None
    phase: SessionPhase = SessionPhase.IDLE
    current_turn_id: str | None = None
    turns: list[ConversationTurn] = field(default_factory=list)
    checkpoint: ParserCheckpoint = field(default_factory=ParserCheckpoint)
    summary: str | None = None
    last_reply: str | None = None
    last_reply_role: str | None = None
    last_tool_name: str | None = None
    tool_calls: dict[str, ToolCallRecord] = field(default_factory=dict)
    pending_permission: PendingPermission | None = None
    subagent_state: SubagentState = field(default_factory=SubagentState)
    structured_reply_turn_id: str | None = None
    structured_permission_key: str | None = None
    revision: int = 0
    history_loaded: bool = False
    clear_detected: bool = False
    interrupted: bool = False
    created_at: datetime = field(default_factory=utc_now)
    last_activity: datetime = field(default_factory=utc_now)

    def current_turn(self) -> ConversationTurn | None:
        if not self.current_turn_id:
            return None
        for turn in reversed(self.turns):
            if turn.turn_id == self.current_turn_id:
                return turn
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "provider": self.provider,
            "workdir": self.workdir,
            "terminal_id": self.terminal_id,
            "claude_session_id": self.claude_session_id,
            "phase": self.phase.value,
            "current_turn_id": self.current_turn_id,
            "turns": [turn.to_dict() for turn in self.turns],
            "checkpoint": self.checkpoint.to_dict(),
            "summary": self.summary,
            "last_reply": self.last_reply,
            "last_reply_role": self.last_reply_role,
            "last_tool_name": self.last_tool_name,
            "tool_calls": {key: value.to_dict() for key, value in self.tool_calls.items()},
            "pending_permission": self.pending_permission.to_dict() if self.pending_permission else None,
            "subagent_state": self.subagent_state.to_dict(),
            "structured_reply_turn_id": self.structured_reply_turn_id,
            "structured_permission_key": self.structured_permission_key,
            "revision": self.revision,
            "history_loaded": self.history_loaded,
            "clear_detected": self.clear_detected,
            "interrupted": self.interrupted,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        session_id = str(data["session_id"])
        return cls(
            session_id=session_id,
            user_id=data.get("user_id"),
            provider=str(data.get("provider", "claude_code")),
            workdir=str(data.get("workdir", ".")),
            terminal_id=data.get("terminal_id"),
            claude_session_id=str(data["claude_session_id"]) if data.get("claude_session_id") is not None else session_id,
            phase=SessionPhase(data.get("phase", SessionPhase.IDLE.value)),
            current_turn_id=data.get("current_turn_id"),
            turns=[ConversationTurn.from_dict(item) for item in data.get("turns", [])],
            checkpoint=ParserCheckpoint.from_dict(data.get("checkpoint")),
            summary=str(data["summary"]) if data.get("summary") is not None else None,
            last_reply=str(data["last_reply"]) if data.get("last_reply") is not None else None,
            last_reply_role=str(data["last_reply_role"]) if data.get("last_reply_role") is not None else None,
            last_tool_name=str(data["last_tool_name"]) if data.get("last_tool_name") is not None else None,
            tool_calls={str(key): ToolCallRecord.from_dict(value) for key, value in dict(data.get("tool_calls", {})).items()},
            pending_permission=PendingPermission.from_dict(data.get("pending_permission")),
            subagent_state=SubagentState.from_dict(data.get("subagent_state")),
            structured_reply_turn_id=str(data["structured_reply_turn_id"]) if data.get("structured_reply_turn_id") is not None else None,
            structured_permission_key=str(data["structured_permission_key"]) if data.get("structured_permission_key") is not None else None,
            revision=int(data.get("revision", 0)),
            history_loaded=bool(data.get("history_loaded", False)),
            clear_detected=bool(data.get("clear_detected", False)),
            interrupted=bool(data.get("interrupted", False)),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else utc_now(),
            last_activity=datetime.fromisoformat(data["last_activity"]) if data.get("last_activity") else utc_now(),
        )
