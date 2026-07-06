from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.user_question_models import UserQuestionPrompt


@dataclass(frozen=True)
class _SubagentToolStateSnapshot:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str | None


@dataclass(frozen=True)
class _ToolStateSnapshot:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str | None
    result: str | None = None
    structured_result: dict | None = None
    subagent_tools: tuple[_SubagentToolStateSnapshot, ...] = ()


@dataclass
class _StructuredSnapshot:
    session_id: str | None
    turn_id: str | None
    reply: str
    session_available: bool
    phase: str | None = None
    pending_permission_key: str | None = None
    pending_permission_tool_use_id: str | None = None
    pending_permission_tool_name: str | None = None
    pending_permission_tool_input: dict | None = None
    cwd: str | None = None
    session_title: str | None = None
    user_id: int | None = None
    tool_states: tuple[_ToolStateSnapshot, ...] = ()
    turn_started_at: datetime | None = None
    turn_ended_at: datetime | None = None


@dataclass(frozen=True)
class StructuredReplyOutput:
    text: str
    turn_id: str


@dataclass(frozen=True)
class StructuredReplyFallbackOutput:
    text: str


@dataclass(frozen=True)
class PermissionRequestOutput:
    text: str
    tool_use_id: str | None
    permission_key: str
    tool_name: str | None = None
    session_id: str | None = None
    tool_input: dict | None = None
    cwd: str | None = None
    session_title: str | None = None
    user_id: int | None = None


@dataclass(frozen=True)
class ProgressUpdateOutput:
    text: str


@dataclass(frozen=True)
class SubagentToolStatusOutput:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str


@dataclass(frozen=True)
class ToolStatusOutput:
    tool_use_id: str
    tool_name: str | None
    tool_input: dict | None
    status: str
    subagent_tools: tuple[SubagentToolStatusOutput, ...] = ()


@dataclass(frozen=True)
class SubagentAggregateStatusOutput:
    message_key: str
    containers: tuple[ToolStatusOutput, ...]


@dataclass(frozen=True)
class TaskListItemStatusOutput:
    task_id: str
    subject: str
    status: str
    active_form: str | None = None


@dataclass(frozen=True)
class TaskListStatusOutput:
    message_key: str
    items: tuple[TaskListItemStatusOutput, ...]


@dataclass(frozen=True)
class FileToolAggregateStatusOutput:
    message_key: str
    tools: tuple[ToolStatusOutput, ...]


@dataclass(frozen=True)
class UserQuestionOutput:
    text: str
    question: UserQuestionPrompt
    session_id: str | None = None
    session_title: str | None = None
    cwd: str | None = None
