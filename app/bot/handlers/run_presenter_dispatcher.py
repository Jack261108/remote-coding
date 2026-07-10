from __future__ import annotations

from typing import TYPE_CHECKING, cast

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.command_user_question import build_user_question_keyboard
from app.bot.handlers.run_display_models import (
    DisplayEvent,
    RenderCommand,
    RenderCommandKind,
    StreamTextDisplayPayload,
    TaskFailedDisplayPayload,
    TaskSucceededDisplayPayload,
    ToolRenderOutput,
    format_task_failed_text,
    format_task_succeeded_text,
    render_command_from_display_event,
    render_command_from_presenter_output,
)
from app.bot.handlers.run_telegram_messenger import RunTelegramMessenger
from app.bot.presenters.chunk_sender import ChunkSender
from app.bot.presenters.permission_message_builder import PermissionPromptInput
from app.bot.presenters.structured_reply_presenter import (
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyFallbackOutput,
    StructuredReplyOutput,
    StructuredReplyPresenter,
    UserQuestionOutput,
)
from app.bot.presenters.tool_message_manager import ToolMessageManager
from app.infra.source_text_normalization import normalize_source_text
from app.services.message_sender import Keyboard
from app.services.permission_callback_registry import AutoApproveOutcome, SessionOrigin
from app.services.permission_gateway import RegisterForButtonConflict, RegisterForButtonOk

if TYPE_CHECKING:
    from app.services.permission_gateway import PermissionGateway


def _to_inline_keyboard_markup(keyboard: Keyboard | InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    if isinstance(keyboard, InlineKeyboardMarkup):
        return keyboard
    service_keyboard = cast(Keyboard, keyboard)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button.text, callback_data=button.callback_data) for button in row] for row in service_keyboard.rows
        ]
    )


class PresenterOutputDispatcher:
    def __init__(
        self,
        *,
        presenter: StructuredReplyPresenter,
        sender: ChunkSender,
        messenger: RunTelegramMessenger,
        tool_message_manager: ToolMessageManager,
        task_id: str,
        permission_gateway: PermissionGateway | None = None,
    ) -> None:
        self._presenter = presenter
        self._sender = sender
        self._messenger = messenger
        self._tool_message_manager = tool_message_manager
        self._task_id = task_id
        self._permission_gateway = permission_gateway
        self._fallback_message: Message | None = None

    async def send_text(self, text: str) -> bool:
        normalized = normalize_source_text(text)
        if not normalized:
            return True
        return await self._messenger.answer_safely(normalized)

    async def push_text(self, text: str) -> bool:
        return await self._sender.push(text, self.send_text)

    async def flush(self) -> bool:
        return await self._sender.flush(self.send_text)

    async def emit_structured_reply(self, output: StructuredReplyOutput, *, flush_before: bool = True) -> None:
        if flush_before:
            await self.flush()

        async def send_structured_text(text: str) -> bool:
            if not text.strip():
                return True
            return await self._messenger.answer_safely(text)

        if self._fallback_message is not None:
            fallback_message = self._fallback_message
            if not output.text.strip():
                return
            edited = await self._messenger.edit_message_safely(fallback_message, output.text)
            if edited:
                self._fallback_message = None
                await self._presenter.acknowledge_delivery(output)
                return
            deleted = await self._messenger.delete_message_safely(fallback_message)
            if not deleted:
                return
            self._fallback_message = None

        push_ok = await self._sender.push(output.text, send_structured_text)
        flush_ok = await self._sender.flush(send_structured_text)
        if push_ok and flush_ok:
            await self._presenter.acknowledge_delivery(output)

    async def emit_presenter_messages(self, *, final: bool = False, log_missing: bool) -> None:
        for output in await self._presenter.poll(task_id=self._task_id, final=final, log_missing=log_missing):
            await self._execute_render_command(render_command_from_presenter_output(output))

    async def execute_display_event(self, event: DisplayEvent, *, lifecycle_message: Message | None = None) -> None:
        await self._execute_render_command(render_command_from_display_event(event), lifecycle_message=lifecycle_message)

    async def _execute_render_command(self, command: RenderCommand, *, lifecycle_message: Message | None = None) -> None:
        if command.flush_before:
            await self.flush()

        if command.kind == RenderCommandKind.BUFFER_TEXT:
            await self._sender.push(cast(str, command.payload), self.send_text)
            return
        if command.kind == RenderCommandKind.EMIT_STRUCTURED_REPLY:
            await self.emit_structured_reply(cast(StructuredReplyOutput, command.payload), flush_before=False)
            return
        if command.kind == RenderCommandKind.SEND_STRUCTURED_FALLBACK:
            await self._send_structured_fallback(cast(StructuredReplyFallbackOutput, command.payload))
            return
        if command.kind == RenderCommandKind.REQUEST_PERMISSION:
            await self._send_permission_request(cast(PermissionRequestOutput, command.payload))
            return
        if command.kind == RenderCommandKind.ASK_USER_QUESTION:
            await self._send_user_question(cast(UserQuestionOutput, command.payload))
            return
        if command.kind == RenderCommandKind.HANDLE_TOOL_STATUS:
            await self._handle_tool_status(cast(ToolRenderOutput, command.payload))
            return
        if command.kind == RenderCommandKind.SEND_PROGRESS_UPDATE:
            await self._send_progress_update(cast(ProgressUpdateOutput, command.payload))
            return
        if command.kind == RenderCommandKind.BUFFER_STREAM_TEXT:
            await self._buffer_stream_text(cast(StreamTextDisplayPayload, command.payload))
            return
        if command.kind == RenderCommandKind.COMPLETE_LIFECYCLE:
            await self._complete_lifecycle(cast(TaskSucceededDisplayPayload, command.payload), lifecycle_message)
            return
        if command.kind == RenderCommandKind.FAIL_LIFECYCLE:
            await self._fail_lifecycle(cast(TaskFailedDisplayPayload, command.payload), lifecycle_message)
            return
        raise TypeError(f"unsupported render command kind: {command.kind}")

    async def _buffer_stream_text(self, payload: StreamTextDisplayPayload) -> None:
        prefix = "[stderr] " if payload.is_stderr else ""
        await self.push_text(f"{prefix}{payload.text}")

    async def _complete_lifecycle(self, payload: TaskSucceededDisplayPayload, lifecycle_message: Message | None) -> None:
        text = format_task_succeeded_text(payload)
        if not await self._messenger.edit_message_safely(lifecycle_message, text):
            await self._messenger.answer_safely(text)

    async def _fail_lifecycle(self, payload: TaskFailedDisplayPayload, lifecycle_message: Message | None) -> None:
        text = format_task_failed_text(payload)
        if not await self._messenger.edit_message_safely(lifecycle_message, text):
            await self._messenger.answer_safely(text)

    async def _send_permission_request(self, output: PermissionRequestOutput) -> None:
        if self._permission_gateway is None or not output.tool_use_id or not output.session_id:
            raise RuntimeError("permission gateway is not configured")

        outcome = await self._permission_gateway.maybe_auto_approve(
            session_id=output.session_id,
            origin=SessionOrigin.OWNED,
            candidate_user_id=output.user_id,
            tool_use_id=output.tool_use_id,
            tool_name=output.tool_name or "unknown tool",
            tool_input=output.tool_input,
        )
        if outcome in {AutoApproveOutcome.APPROVED, AutoApproveOutcome.APPROVAL_UNKNOWN}:
            await self._presenter.acknowledge_delivery(output)
            return

        result = await self._permission_gateway.register_for_button(
            tool_use_id=output.tool_use_id,
            session_id=output.session_id,
            origin=SessionOrigin.OWNED,
            candidate_user_id=output.user_id,
        )
        if isinstance(result, RegisterForButtonConflict):
            sent = await self._messenger.answer_safely(
                result.advisory_text,
                reply_markup=_to_inline_keyboard_markup(result.keyboard),
            )
            if sent:
                await self._presenter.acknowledge_delivery(output)
            return
        if not isinstance(result, RegisterForButtonOk):
            raise RuntimeError("unexpected permission gateway registration result")
        keyboard = _to_inline_keyboard_markup(result.keyboard)
        text = self._permission_gateway.message_builder.build_permission_prompt(
            PermissionPromptInput(
                tool_name=output.tool_name or "unknown tool",
                tool_input=output.tool_input,
                cwd=output.cwd or "",
                session_id=output.session_id,
                session_title=output.session_title,
            )
        )
        # Try editing the existing tool status message into the permission prompt
        edited, edited_message = await self._tool_message_manager.edit_with_keyboard(
            tool_use_id=output.tool_use_id,
            text=text,
            reply_markup=keyboard,
        )
        if edited and edited_message:
            # Store message info for terminal approval sync
            await self._permission_gateway.registry.update_telegram_message(
                token=result.token,
                chat_id=edited_message.chat.id,
                message_id=edited_message.message_id,
                message_text=text,
            )
            await self._presenter.acknowledge_delivery(output)
            return
        sent_message = await self._messenger.send_message_safely(text, reply_markup=keyboard)
        if sent_message:
            # Store message info for terminal approval sync
            await self._permission_gateway.registry.update_telegram_message(
                token=result.token,
                chat_id=sent_message.chat.id,
                message_id=sent_message.message_id,
                message_text=text,
            )
            await self._presenter.acknowledge_delivery(output)

    async def _send_user_question(self, output: UserQuestionOutput) -> None:
        sent = await self._messenger.answer_safely(
            output.text,
            reply_markup=build_user_question_keyboard(output),
        )
        if sent:
            await self._presenter.acknowledge_delivery(output)

    async def _handle_tool_status(self, output: ToolRenderOutput) -> None:
        await self._tool_message_manager.handle(output)

    async def _send_progress_update(self, output: ProgressUpdateOutput) -> None:
        await self._messenger.answer_safely(output.text)

    async def _send_structured_fallback(self, output: StructuredReplyFallbackOutput) -> None:
        sent_message = await self._messenger.send_message_safely(output.text)
        if sent_message is not None:
            self._fallback_message = sent_message
        else:
            self._presenter.mark_fallback_delivery_failed()
