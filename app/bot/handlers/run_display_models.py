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


class DisplayEventKind(StrEnum):
    TEXT = "text"
    STRUCTURED_REPLY = "structured_reply"
    STRUCTURED_FALLBACK = "structured_fallback"
    PERMISSION_REQUEST = "permission_request"
    USER_QUESTION = "user_question"
    TOOL_STATUS = "tool_status"
    PROGRESS_UPDATE = "progress_update"


class RenderCommandKind(StrEnum):
    BUFFER_TEXT = "buffer_text"
    EMIT_STRUCTURED_REPLY = "emit_structured_reply"
    SEND_STRUCTURED_FALLBACK = "send_structured_fallback"
    REQUEST_PERMISSION = "request_permission"
    ASK_USER_QUESTION = "ask_user_question"
    HANDLE_TOOL_STATUS = "handle_tool_status"
    SEND_PROGRESS_UPDATE = "send_progress_update"


@dataclass(frozen=True)
class DisplayEvent:
    kind: DisplayEventKind
    payload: RunPresenterOutput


@dataclass(frozen=True)
class RenderCommand:
    kind: RenderCommandKind
    payload: RunPresenterOutput
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
    raise TypeError(f"unsupported display event kind: {event.kind}")


def render_command_from_presenter_output(output: object) -> RenderCommand:
    return render_command_from_display_event(display_event_from_presenter_output(output))
