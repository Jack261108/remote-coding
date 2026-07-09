from __future__ import annotations

import pytest

from app.bot.handlers.run_display_models import (
    DisplayEventKind,
    RenderCommandKind,
    display_event_from_presenter_output,
    render_command_from_presenter_output,
)
from app.bot.presenters.structured_reply_models import (
    FileToolAggregateStatusOutput,
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyFallbackOutput,
    StructuredReplyOutput,
    SubagentAggregateStatusOutput,
    TaskListItemStatusOutput,
    TaskListStatusOutput,
    ToolStatusOutput,
    UserQuestionOutput,
)
from app.domain.user_question_models import UserQuestionPrompt


def assert_maps_to(output: object, *, display_kind: DisplayEventKind, command_kind: RenderCommandKind, flush_before: bool = True) -> None:
    event = display_event_from_presenter_output(output)
    command = render_command_from_presenter_output(output)

    assert event.kind == display_kind
    assert event.payload is output
    assert command.kind == command_kind
    assert command.payload is output
    assert command.flush_before is flush_before


def test_text_output_maps_to_buffer_command_without_flush() -> None:
    output = "hello"

    assert_maps_to(output, display_kind=DisplayEventKind.TEXT, command_kind=RenderCommandKind.BUFFER_TEXT, flush_before=False)


def test_structured_reply_output_maps_to_emit_command() -> None:
    output = StructuredReplyOutput(text="reply", turn_id="turn-1")

    assert_maps_to(output, display_kind=DisplayEventKind.STRUCTURED_REPLY, command_kind=RenderCommandKind.EMIT_STRUCTURED_REPLY)


def test_structured_fallback_output_maps_to_fallback_command() -> None:
    output = StructuredReplyFallbackOutput(text="fallback")

    assert_maps_to(output, display_kind=DisplayEventKind.STRUCTURED_FALLBACK, command_kind=RenderCommandKind.SEND_STRUCTURED_FALLBACK)


def test_permission_request_output_maps_to_permission_command() -> None:
    output = PermissionRequestOutput(text="permission", tool_use_id="tool-1", permission_key="permission-1")

    assert_maps_to(output, display_kind=DisplayEventKind.PERMISSION_REQUEST, command_kind=RenderCommandKind.REQUEST_PERMISSION)


def test_user_question_output_maps_to_question_command() -> None:
    output = UserQuestionOutput(
        text="question",
        question=UserQuestionPrompt(tool_use_id="tool-1", question_index=1, total_questions=1, question="Pick one?"),
    )

    assert_maps_to(output, display_kind=DisplayEventKind.USER_QUESTION, command_kind=RenderCommandKind.ASK_USER_QUESTION)


def test_progress_update_output_maps_to_progress_command() -> None:
    output = ProgressUpdateOutput(text="progress")

    assert_maps_to(output, display_kind=DisplayEventKind.PROGRESS_UPDATE, command_kind=RenderCommandKind.SEND_PROGRESS_UPDATE)


def test_tool_status_outputs_map_to_tool_status_command() -> None:
    tool = ToolStatusOutput(tool_use_id="tool-1", tool_name="Bash", tool_input={"command": "pwd"}, status="running")
    outputs = [
        tool,
        SubagentAggregateStatusOutput(message_key="subagents", containers=(tool,)),
        TaskListStatusOutput(
            message_key="tasks",
            items=(TaskListItemStatusOutput(task_id="1", subject="Do thing", status="pending"),),
        ),
        FileToolAggregateStatusOutput(message_key="files", tools=(tool,)),
    ]

    for output in outputs:
        assert_maps_to(output, display_kind=DisplayEventKind.TOOL_STATUS, command_kind=RenderCommandKind.HANDLE_TOOL_STATUS)


def test_unknown_presenter_output_raises_type_error() -> None:
    with pytest.raises(TypeError, match="unsupported presenter output"):
        display_event_from_presenter_output(object())

    with pytest.raises(TypeError, match="unsupported presenter output"):
        render_command_from_presenter_output(object())
