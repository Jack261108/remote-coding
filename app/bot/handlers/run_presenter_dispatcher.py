from __future__ import annotations

from typing import TYPE_CHECKING

from app.bot.handlers.command_permission import build_permission_keyboard
from app.bot.handlers.command_user_question import build_user_question_keyboard
from app.bot.handlers.run_telegram_messenger import RunTelegramMessenger
from app.bot.presenters.chunk_sender import ChunkSender
from app.bot.presenters.structured_reply_presenter import (
    FileToolAggregateStatusOutput,
    PermissionRequestOutput,
    ProgressUpdateOutput,
    StructuredReplyOutput,
    StructuredReplyPresenter,
    SubagentAggregateStatusOutput,
    TaskListStatusOutput,
    ToolStatusOutput,
    UserQuestionOutput,
    normalize_stream_text,
)
from app.bot.presenters.tool_message_manager import ToolMessageManager

if TYPE_CHECKING:
    from app.services.permission_callback_registry import PermissionCallbackRegistry


class PresenterOutputDispatcher:
    def __init__(
        self,
        *,
        presenter: StructuredReplyPresenter,
        sender: ChunkSender,
        messenger: RunTelegramMessenger,
        tool_message_manager: ToolMessageManager,
        task_id: str,
        permission_callback_registry: PermissionCallbackRegistry | None = None,
    ) -> None:
        self._presenter = presenter
        self._sender = sender
        self._messenger = messenger
        self._tool_message_manager = tool_message_manager
        self._task_id = task_id
        self._permission_callback_registry = permission_callback_registry

    async def send_text(self, text: str) -> bool:
        normalized = normalize_stream_text(text)
        if not normalized:
            return True
        return await self._messenger.answer_safely(normalized)

    async def push_text(self, text: str) -> bool:
        return await self._sender.push(text, self.send_text)

    async def flush(self) -> bool:
        return await self._sender.flush(self.send_text)

    async def emit_presenter_messages(self, *, final: bool = False, log_missing: bool) -> None:
        for output in await self._presenter.poll(task_id=self._task_id, final=final, log_missing=log_missing):
            if isinstance(output, PermissionRequestOutput):
                await self.flush()
                keyboard = (
                    build_permission_keyboard(
                        tool_use_id=output.tool_use_id, permission_callback_registry=self._permission_callback_registry
                    )
                    if output.tool_use_id and self._permission_callback_registry is not None
                    else None
                )
                # Try editing the existing tool status message into the permission prompt
                edited = False
                if output.tool_use_id:
                    edited = await self._tool_message_manager.edit_with_keyboard(
                        tool_use_id=output.tool_use_id,
                        text=output.text,
                        reply_markup=keyboard,
                    )
                if edited:
                    await self._presenter.acknowledge_delivery(output)
                    continue
                sent = await self._messenger.answer_safely(output.text, reply_markup=keyboard)
                if sent:
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
                await self.flush()

                async def send_structured_text(text: str) -> bool:
                    normalized = normalize_stream_text(text)
                    if not normalized:
                        return True
                    return await self._messenger.answer_safely(normalized)

                push_ok = await self._sender.push(output.text, send_structured_text)
                flush_ok = await self._sender.flush(send_structured_text)
                if push_ok and flush_ok:
                    await self._presenter.acknowledge_delivery(output)
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
            await self._sender.push(output, self.send_text)
