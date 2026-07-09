from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.handlers.run_display_models import RenderCommand, RenderCommandKind
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

    async def answer_safely(self, text: str, reply_markup: object = None) -> bool:
        self.answers.append(text)
        return True

    async def send_message_safely(self, text: str, reply_markup: object = None):
        self.sent_messages.append(text)
        return MagicMock()


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
