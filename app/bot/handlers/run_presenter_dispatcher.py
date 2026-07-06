from __future__ import annotations

from typing import TYPE_CHECKING, cast

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.command_user_question import build_user_question_keyboard
from app.bot.handlers.run_telegram_messenger import RunTelegramMessenger
from app.bot.presenters.chunk_sender import ChunkSender
from app.bot.presenters.permission_message_builder import PermissionPromptInput
from app.bot.presenters.structured_reply_presenter import (
    FileToolAggregateStatusOutput,
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyFallbackOutput,
    StructuredReplyOutput,
    StructuredReplyPresenter,
    SubagentAggregateStatusOutput,
    TaskListStatusOutput,
    ToolStatusOutput,
    UserQuestionOutput,
    normalize_stream_text,
)
from app.bot.presenters.tool_message_manager import ToolMessageManager
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
        normalized = normalize_stream_text(text)
        if not normalized:
            return True
        return await self._messenger.answer_safely(normalized)

    async def push_text(self, text: str) -> bool:
        return await self._sender.push(text, self.send_text)

    async def flush(self) -> bool:
        return await self._sender.flush(self.send_text)

    async def emit_structured_reply(self, output: StructuredReplyOutput) -> None:
        await self.flush()

        async def send_structured_text(text: str) -> bool:
            normalized = normalize_stream_text(text)
            if not normalized:
                return True
            return await self._messenger.answer_safely(normalized)

        if self._fallback_message is not None:
            fallback_message = self._fallback_message
            normalized = normalize_stream_text(output.text)
            edited = await self._messenger.edit_message_safely(fallback_message, normalized)
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
            if isinstance(output, PermissionRequestOutput):
                await self.flush()
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
                    continue

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
                    continue
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
                    continue
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
                continue
            if isinstance(output, UserQuestionOutput):
                await self.flush()
                sent = await self._messenger.answer_safely(
                    output.text,
                    reply_markup=build_user_question_keyboard(output),
                )
                if sent:
                    await self._presenter.acknowledge_delivery(output)
                continue
            if isinstance(output, StructuredReplyOutput):
                await self.emit_structured_reply(output)
                continue
            if isinstance(
                output,
                (ToolStatusOutput, SubagentAggregateStatusOutput, TaskListStatusOutput, FileToolAggregateStatusOutput),
            ):
                await self.flush()
                await self._tool_message_manager.handle(output)
                continue
            if isinstance(output, ProgressUpdateOutput):
                await self.flush()
                await self._messenger.answer_safely(output.text)
                continue
            if isinstance(output, StructuredReplyFallbackOutput):
                await self.flush()
                sent_message = await self._messenger.send_message_safely(output.text)
                if sent_message is not None:
                    self._fallback_message = sent_message
                else:
                    self._presenter.mark_fallback_delivery_failed()
                continue
            await self._sender.push(output, self.send_text)
