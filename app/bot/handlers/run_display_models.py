from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from app.bot.presenters.structured_reply_models import (
    FileToolAggregateStatusOutput,
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyFallbackOutput,
    StructuredReplyOutput,
    SubagentAggregateStatusOutput,
    TaskListStatusOutput,
    ToolStatusOutput,
    UserQuestionOutput,
)
from app.domain.models import EventType
from app.infra.text_formatting import short_id

RunPresenterOutput: TypeAlias = (
    str
    | StructuredReplyOutput
    | StructuredReplyFallbackOutput
    | PermissionRequestOutput
    | ProgressUpdateOutput
    | ToolStatusOutput
    | SubagentAggregateStatusOutput
    | TaskListStatusOutput
    | FileToolAggregateStatusOutput
    | UserQuestionOutput
)

ToolRenderOutput: TypeAlias = ToolStatusOutput | SubagentAggregateStatusOutput | TaskListStatusOutput | FileToolAggregateStatusOutput


@dataclass(frozen=True)
class StreamTextDisplayPayload:
    text: str
    is_stderr: bool = False


@dataclass(frozen=True)
class TaskSucceededDisplayPayload:
    task_id: str
    duration: str
    truncated: bool
    exit_code: int | None


@dataclass(frozen=True)
class TaskFailedDisplayPayload:
    event_type: EventType
    task_id: str
    error_text: str
    duration: str
    truncated: bool


RunDisplayPayload: TypeAlias = RunPresenterOutput | StreamTextDisplayPayload | TaskSucceededDisplayPayload | TaskFailedDisplayPayload


class DisplayEventKind(StrEnum):
    TEXT = "text"
    STRUCTURED_REPLY = "structured_reply"
    STRUCTURED_FALLBACK = "structured_fallback"
    PERMISSION_REQUEST = "permission_request"
    USER_QUESTION = "user_question"
    TOOL_STATUS = "tool_status"
    PROGRESS_UPDATE = "progress_update"
    STREAM_TEXT = "stream_text"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"


class RenderCommandKind(StrEnum):
    BUFFER_TEXT = "buffer_text"
    EMIT_STRUCTURED_REPLY = "emit_structured_reply"
    SEND_STRUCTURED_FALLBACK = "send_structured_fallback"
    REQUEST_PERMISSION = "request_permission"
    ASK_USER_QUESTION = "ask_user_question"
    HANDLE_TOOL_STATUS = "handle_tool_status"
    SEND_PROGRESS_UPDATE = "send_progress_update"
    BUFFER_STREAM_TEXT = "buffer_stream_text"
    COMPLETE_LIFECYCLE = "complete_lifecycle"
    FAIL_LIFECYCLE = "fail_lifecycle"


@dataclass(frozen=True)
class DisplayEvent:
    kind: DisplayEventKind
    payload: RunDisplayPayload


@dataclass(frozen=True)
class RenderCommand:
    kind: RenderCommandKind
    payload: RunDisplayPayload
    flush_before: bool = False


def display_event_from_presenter_output(output: object) -> DisplayEvent:
    if isinstance(output, str):
        return DisplayEvent(kind=DisplayEventKind.TEXT, payload=output)
    if isinstance(output, StructuredReplyOutput):
        return DisplayEvent(kind=DisplayEventKind.STRUCTURED_REPLY, payload=output)
    if isinstance(output, StructuredReplyFallbackOutput):
        return DisplayEvent(kind=DisplayEventKind.STRUCTURED_FALLBACK, payload=output)
    if isinstance(output, PermissionRequestOutput):
        return DisplayEvent(kind=DisplayEventKind.PERMISSION_REQUEST, payload=output)
    if isinstance(output, UserQuestionOutput):
        return DisplayEvent(kind=DisplayEventKind.USER_QUESTION, payload=output)
    if isinstance(output, ProgressUpdateOutput):
        return DisplayEvent(kind=DisplayEventKind.PROGRESS_UPDATE, payload=output)
    if isinstance(output, (ToolStatusOutput, SubagentAggregateStatusOutput, TaskListStatusOutput, FileToolAggregateStatusOutput)):
        return DisplayEvent(kind=DisplayEventKind.TOOL_STATUS, payload=output)
    raise TypeError(f"unsupported presenter output: {type(output).__name__}")


def render_command_from_display_event(event: DisplayEvent) -> RenderCommand:
    if event.kind == DisplayEventKind.TEXT:
        return RenderCommand(kind=RenderCommandKind.BUFFER_TEXT, payload=event.payload)
    if event.kind == DisplayEventKind.STRUCTURED_REPLY:
        return RenderCommand(kind=RenderCommandKind.EMIT_STRUCTURED_REPLY, payload=event.payload, flush_before=True)
    if event.kind == DisplayEventKind.STRUCTURED_FALLBACK:
        return RenderCommand(kind=RenderCommandKind.SEND_STRUCTURED_FALLBACK, payload=event.payload, flush_before=True)
    if event.kind == DisplayEventKind.PERMISSION_REQUEST:
        return RenderCommand(kind=RenderCommandKind.REQUEST_PERMISSION, payload=event.payload, flush_before=True)
    if event.kind == DisplayEventKind.USER_QUESTION:
        return RenderCommand(kind=RenderCommandKind.ASK_USER_QUESTION, payload=event.payload, flush_before=True)
    if event.kind == DisplayEventKind.TOOL_STATUS:
        return RenderCommand(kind=RenderCommandKind.HANDLE_TOOL_STATUS, payload=event.payload, flush_before=True)
    if event.kind == DisplayEventKind.PROGRESS_UPDATE:
        return RenderCommand(kind=RenderCommandKind.SEND_PROGRESS_UPDATE, payload=event.payload, flush_before=True)
    if event.kind == DisplayEventKind.STREAM_TEXT:
        return RenderCommand(kind=RenderCommandKind.BUFFER_STREAM_TEXT, payload=event.payload)
    if event.kind == DisplayEventKind.TASK_SUCCEEDED:
        return RenderCommand(kind=RenderCommandKind.COMPLETE_LIFECYCLE, payload=event.payload, flush_before=True)
    if event.kind == DisplayEventKind.TASK_FAILED:
        return RenderCommand(kind=RenderCommandKind.FAIL_LIFECYCLE, payload=event.payload, flush_before=True)
    raise TypeError(f"unsupported display event kind: {event.kind}")


def format_task_succeeded_text(payload: TaskSucceededDisplayPayload) -> str:
    display_task_id = short_id(payload.task_id)
    parts = [f"✅ 完成 [{display_task_id}] {payload.duration}"]
    if payload.truncated:
        parts.append("（输出已截断）")
    return " ".join(parts)


def format_task_failed_text(payload: TaskFailedDisplayPayload) -> str:
    display_task_id = short_id(payload.task_id)
    icon_map = {
        EventType.FAILED: "❌",
        EventType.TIMEOUT: "⏰",
        EventType.CANCELED: "🚫",
    }
    label_map = {
        EventType.FAILED: "失败",
        EventType.TIMEOUT: "超时",
        EventType.CANCELED: "已取消",
    }
    icon = icon_map.get(payload.event_type, "❌")
    label = label_map.get(payload.event_type, "错误")
    parts = [f"{icon} {label} [{display_task_id}] {payload.duration}"]
    if payload.error_text and payload.error_text != "-":
        parts.append(f"\n{payload.error_text}")
    if payload.truncated:
        parts.append("（输出已截断）")
    return "".join(parts)


def render_command_from_presenter_output(output: object) -> RenderCommand:
    return render_command_from_display_event(display_event_from_presenter_output(output))
