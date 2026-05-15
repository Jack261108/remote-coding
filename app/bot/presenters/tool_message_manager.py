from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram.enums import ParseMode
from aiogram.types import Message

from app.bot.presenters.structured_reply_presenter import (
    SubagentAggregateStatusOutput,
    TaskListStatusOutput,
    ToolStatusOutput,
    build_subagent_aggregate_status_message,
    build_task_list_status_message,
    build_tool_status_message,
    build_tool_task_list_message,
)
from app.bot.presenters.telegram_formatting import render_markdownish_to_telegram_html, split_telegram_html

logger = logging.getLogger(__name__)


@dataclass
class _TrackedToolMessage:
    message: Message


class ToolMessageManager:
    def __init__(self, *, root_message: Message, task_id: str, user_id: int, provider: str) -> None:
        self._root_message = root_message
        self._task_id = task_id
        self._user_id = user_id
        self._provider = provider
        self._messages: dict[str, _TrackedToolMessage] = {}

    async def handle(self, output: ToolStatusOutput | SubagentAggregateStatusOutput | TaskListStatusOutput) -> None:
        if isinstance(output, SubagentAggregateStatusOutput):
            message_key = output.message_key
            text = build_subagent_aggregate_status_message(output)
        elif isinstance(output, TaskListStatusOutput):
            message_key = output.message_key
            text = build_task_list_status_message(output)
        elif output.subagent_tools:
            message_key = output.tool_use_id
            text = build_tool_task_list_message(output)
        else:
            message_key = output.tool_use_id
            text = build_tool_status_message(
                tool_name=output.tool_name,
                tool_input=output.tool_input,
                status=output.status,
            )
        existing = self._messages.get(message_key)
        if existing is None:
            await self._send_and_track(message_key, text)
            return

        edited = await self._edit(existing.message, text, tool_use_id=message_key)
        if edited:
            return
        await self._send_and_track(message_key, text)

    async def _send_and_track(self, tool_use_id: str, text: str) -> None:
        sent = await self._send(text, tool_use_id=tool_use_id)
        if sent is not None:
            self._messages[tool_use_id] = _TrackedToolMessage(message=sent)

    async def _send(self, text: str, *, tool_use_id: str) -> Message | None:
        try:
            rendered = self._render(text)
            return await self._root_message.answer(rendered, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception(
                "telegram tool message send failed",
                extra={
                    "task_id": self._task_id,
                    "user_id": self._user_id,
                    "provider": self._provider,
                    "tool_use_id": tool_use_id,
                },
            )
            return None

    async def _edit(self, message: Message, text: str, *, tool_use_id: str) -> bool:
        try:
            rendered = self._render(text)
            await message.edit_text(rendered, parse_mode=ParseMode.HTML)
            return True
        except Exception:
            logger.exception(
                "telegram tool message edit failed",
                extra={
                    "task_id": self._task_id,
                    "user_id": self._user_id,
                    "provider": self._provider,
                    "tool_use_id": tool_use_id,
                },
            )
            return False

    def _render(self, text: str) -> str:
        rendered = render_markdownish_to_telegram_html(text)
        return split_telegram_html(rendered, 4096)[0]
