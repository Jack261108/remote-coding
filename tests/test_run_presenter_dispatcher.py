from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.handlers.run_display_models import (
    DisplayEvent,
    DisplayEventKind,
    RenderCommand,
    RenderCommandKind,
    StreamTextDisplayPayload,
    TaskFailedDisplayPayload,
    TaskSucceededDisplayPayload,
)
from app.bot.handlers.run_presenter_dispatcher import PresenterOutputDispatcher
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
from app.domain.user_question_models import UserQuestionPrompt
from app.services.permission_callback_registry import AutoApproveOutcome


class _RecordingSender:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.pushed_texts: list[str] = []

    async def push(self, text: str, send_fn) -> bool:
        self.events.append("push")
        self.pushed_texts.append(text)
        await send_fn(text)
        return True

    async def flush(self, send_fn) -> bool:
        self.events.append("flush")
        return True


class _RecordingMessenger:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.sent_messages: list[str] = []
        self.edits: list[tuple[object | None, str]] = []
        self.edit_result = True

    async def answer_safely(self, text: str, reply_markup: object = None) -> bool:
        self.answers.append(text)
        return True

    async def send_message_safely(self, text: str, reply_markup: object = None):
        self.sent_messages.append(text)
        return MagicMock()

    async def edit_message_safely(self, message: object | None, text: str) -> bool:
        self.edits.append((message, text))
        return self.edit_result


class _RecordingToolManager:
    def __init__(self) -> None:
        self.handled: list[object] = []

    async def handle(self, output: object) -> None:
        self.handled.append(output)


def _build_dispatcher() -> tuple[PresenterOutputDispatcher, _RecordingSender, _RecordingMessenger, _RecordingToolManager, MagicMock]:
    presenter = MagicMock()
    presenter.acknowledge_delivery = AsyncMock()
    presenter.mark_fallback_delivery_failed = MagicMock()
    sender = _RecordingSender()
    messenger = _RecordingMessenger()
    tool_manager = _RecordingToolManager()
    permission_gateway = MagicMock()
    permission_gateway.maybe_auto_approve = AsyncMock(return_value=AutoApproveOutcome.APPROVED)
    dispatcher = PresenterOutputDispatcher(
        presenter=presenter,
        sender=sender,
        messenger=messenger,
        tool_message_manager=tool_manager,
        task_id="t1",
        permission_gateway=permission_gateway,
    )
    return dispatcher, sender, messenger, tool_manager, presenter


async def test_stream_text_display_event_pushes_without_flush() -> None:
    dispatcher, sender, messenger, _, _ = _build_dispatcher()

    await dispatcher.execute_display_event(
        DisplayEvent(
            kind=DisplayEventKind.STREAM_TEXT,
            payload=StreamTextDisplayPayload(text="  raw text  "),
        )
    )

    assert sender.events == ["push"]
    assert sender.pushed_texts == ["  raw text  "]
    assert messenger.answers == ["raw text"]


async def test_stderr_display_event_adds_prefix_without_flush() -> None:
    dispatcher, sender, messenger, _, _ = _build_dispatcher()

    await dispatcher.execute_display_event(
        DisplayEvent(
            kind=DisplayEventKind.STREAM_TEXT,
            payload=StreamTextDisplayPayload(text="boom", is_stderr=True),
        )
    )

    assert sender.events == ["push"]
    assert sender.pushed_texts == ["[stderr] boom"]
    assert messenger.answers == ["[stderr] boom"]


async def test_task_succeeded_display_event_edits_lifecycle_after_flush() -> None:
    dispatcher, sender, messenger, _, _ = _build_dispatcher()
    lifecycle_message = MagicMock()

    await dispatcher.execute_display_event(
        DisplayEvent(
            kind=DisplayEventKind.TASK_SUCCEEDED,
            payload=TaskSucceededDisplayPayload(task_id="task-123456", duration="1.25s", truncated=True, exit_code=0),
        ),
        lifecycle_message=lifecycle_message,
    )

    assert sender.events == ["flush"]
    assert messenger.edits == [(lifecycle_message, "✅ 完成 [task-123] 1.25s （输出已截断）")]
    assert messenger.answers == []


async def test_task_succeeded_display_event_falls_back_to_answer_when_edit_fails() -> None:
    dispatcher, sender, messenger, _, _ = _build_dispatcher()
    lifecycle_message = MagicMock()
    messenger.edit_result = False

    await dispatcher.execute_display_event(
        DisplayEvent(
            kind=DisplayEventKind.TASK_SUCCEEDED,
            payload=TaskSucceededDisplayPayload(task_id="task-123456", duration="1.25s", truncated=False, exit_code=7),
        ),
        lifecycle_message=lifecycle_message,
    )

    assert sender.events == ["flush"]
    assert messenger.edits == [(lifecycle_message, "✅ 完成 [task-123] 1.25s")]
    assert messenger.answers == ["✅ 完成 [task-123] 1.25s"]


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (EventType.FAILED, "❌ 失败 [task-123] 2.00s\nboom（输出已截断）"),
        (EventType.TIMEOUT, "⏰ 超时 [task-123] 2.00s\nboom（输出已截断）"),
        (EventType.CANCELED, "🚫 已取消 [task-123] 2.00s\nboom（输出已截断）"),
    ],
)
async def test_task_failed_display_events_render_existing_text(event_type: EventType, expected: str) -> None:
    dispatcher, sender, messenger, _, _ = _build_dispatcher()
    lifecycle_message = MagicMock()

    await dispatcher.execute_display_event(
        DisplayEvent(
            kind=DisplayEventKind.TASK_FAILED,
            payload=TaskFailedDisplayPayload(
                event_type=event_type,
                task_id="task-123456",
                error_text="boom",
                duration="2.00s",
                truncated=True,
            ),
        ),
        lifecycle_message=lifecycle_message,
    )

    assert sender.events == ["flush"]
    assert messenger.edits == [(lifecycle_message, expected)]
    assert messenger.answers == []


async def test_task_failed_display_event_without_message_falls_back_and_hides_dash_error() -> None:
    dispatcher, sender, messenger, _, _ = _build_dispatcher()
    messenger.edit_result = False

    await dispatcher.execute_display_event(
        DisplayEvent(
            kind=DisplayEventKind.TASK_FAILED,
            payload=TaskFailedDisplayPayload(
                event_type=EventType.FAILED,
                task_id="task-123456",
                error_text="-",
                duration="2.00s",
                truncated=False,
            ),
        )
    )

    assert sender.events == ["flush"]
    assert messenger.edits == [(None, "❌ 失败 [task-123] 2.00s")]
    assert messenger.answers == ["❌ 失败 [task-123] 2.00s"]


async def test_buffer_text_pushes_without_flush() -> None:
    dispatcher, sender, messenger, _, _ = _build_dispatcher()

    await dispatcher._execute_render_command(RenderCommand(kind=RenderCommandKind.BUFFER_TEXT, payload="  raw text  "))

    assert sender.events == ["push"]
    assert messenger.answers == ["raw text"]


async def test_emit_structured_reply_flushes_once_before_sending() -> None:
    dispatcher, sender, messenger, _, presenter = _build_dispatcher()

    await dispatcher._execute_render_command(
        RenderCommand(
            kind=RenderCommandKind.EMIT_STRUCTURED_REPLY,
            payload=StructuredReplyOutput(text="reply", turn_id="turn-1"),
            flush_before=True,
        )
    )

    # Pre-flush from _execute_render_command, then emit's internal push + flush.
    # emit_structured_reply must NOT flush again at its own start (flush_before=False).
    assert sender.events == ["flush", "push", "flush"]
    assert messenger.answers == ["reply"]
    presenter.acknowledge_delivery.assert_awaited_once()


async def test_send_structured_fallback_flushes_then_sends() -> None:
    dispatcher, sender, messenger, _, _ = _build_dispatcher()

    await dispatcher._execute_render_command(
        RenderCommand(
            kind=RenderCommandKind.SEND_STRUCTURED_FALLBACK,
            payload=StructuredReplyFallbackOutput(text="fb"),
            flush_before=True,
        )
    )

    assert sender.events == ["flush"]
    assert messenger.sent_messages == ["fb"]
    assert dispatcher._fallback_message is not None


async def test_send_progress_update_flushes_then_answers() -> None:
    dispatcher, sender, messenger, _, _ = _build_dispatcher()

    await dispatcher._execute_render_command(
        RenderCommand(
            kind=RenderCommandKind.SEND_PROGRESS_UPDATE,
            payload=ProgressUpdateOutput(text="pg"),
            flush_before=True,
        )
    )

    assert sender.events == ["flush"]
    assert messenger.answers == ["pg"]


async def test_ask_user_question_flushes_then_answers_and_acks() -> None:
    dispatcher, sender, messenger, _, presenter = _build_dispatcher()

    await dispatcher._execute_render_command(
        RenderCommand(
            kind=RenderCommandKind.ASK_USER_QUESTION,
            payload=UserQuestionOutput(
                text="q",
                question=UserQuestionPrompt(tool_use_id="u1", question_index=1, total_questions=1, question="?"),
            ),
            flush_before=True,
        )
    )

    assert sender.events == ["flush"]
    assert messenger.answers == ["q"]
    presenter.acknowledge_delivery.assert_awaited_once()


async def test_request_permission_auto_approve_acks_without_prompt() -> None:
    dispatcher, sender, messenger, _, presenter = _build_dispatcher()

    await dispatcher._execute_render_command(
        RenderCommand(
            kind=RenderCommandKind.REQUEST_PERMISSION,
            payload=PermissionRequestOutput(text="p", tool_use_id="u1", permission_key="k", session_id="s1"),
            flush_before=True,
        )
    )

    assert sender.events == ["flush"]
    assert messenger.answers == []
    assert messenger.sent_messages == []
    presenter.acknowledge_delivery.assert_awaited_once()


@pytest.mark.parametrize(
    "output",
    [
        ToolStatusOutput(tool_use_id="u1", tool_name="Bash", tool_input={}, status="running"),
        SubagentAggregateStatusOutput(message_key="m", containers=()),
        TaskListStatusOutput(message_key="m", items=()),
        FileToolAggregateStatusOutput(message_key="m", tools=()),
    ],
)
async def test_tool_status_variants_flush_then_handle(output: object) -> None:
    dispatcher, sender, messenger, tool_manager, _ = _build_dispatcher()

    await dispatcher._execute_render_command(RenderCommand(kind=RenderCommandKind.HANDLE_TOOL_STATUS, payload=output, flush_before=True))

    assert sender.events == ["flush"]
    assert tool_manager.handled == [output]
    assert messenger.answers == []
