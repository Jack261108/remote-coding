from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.types import Message

from app.bot.handlers.run_presenter_dispatcher import PresenterOutputDispatcher
from app.bot.handlers.run_telegram_messenger import RunTelegramMessenger
from app.bot.presenters.structured_reply_presenter import StructuredReplyPresenter, normalize_stream_text
from app.domain.models import EventType
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


async def _load_status_summary(task_service: TaskService, task_id: str, user_id: int) -> tuple[str, bool]:
    status = await task_service.get_status(task_id, user_id)
    duration = f"{status.duration_sec:.2f}s" if status and status.duration_sec is not None else "-"
    truncated = bool(status and status.output_truncated)
    return duration, truncated


def _build_created_message(*, task_id: str, provider: str, session_id: str) -> str:
    return (
        "任务已接收\n"
        f"task_id: {task_id}\n"
        f"provider: {provider}\n"
        f"session_id: {session_id}\n"
        "status: 等待启动"
    )


def _build_started_message(*, task_id: str) -> str:
    return "\n".join(["任务开始执行", f"task_id: {task_id}", "status: 正在处理"])


def _build_success_message(*, task_id: str, exit_code: int | None, duration: str, truncated: bool) -> str:
    lines = [
        "任务执行完成",
        f"task_id: {task_id}",
        "status: 成功",
        f"exit_code: {exit_code if exit_code is not None else '-'}",
        f"duration: {duration}",
    ]
    if truncated:
        lines.append("output: truncated")
    return "\n".join(lines)


def _build_error_message(*, event_type: EventType, task_id: str, error_text: str, duration: str, truncated: bool) -> str:
    heading_map = {
        EventType.FAILED: "任务执行失败",
        EventType.TIMEOUT: "任务执行超时",
        EventType.CANCELED: "任务已取消",
    }
    status_map = {
        EventType.FAILED: "失败",
        EventType.TIMEOUT: "超时",
        EventType.CANCELED: "已取消",
    }
    lines = [
        heading_map[event_type],
        f"task_id: {task_id}",
        f"status: {status_map[event_type]}",
        f"error: {error_text}",
        f"duration: {duration}",
    ]
    if truncated:
        lines.append("output: truncated")
    return "\n".join(lines)


class RunEventStreamer:
    def __init__(
        self,
        *,
        start: Any,
        task_service: TaskService,
        user_id: int,
        presenter: StructuredReplyPresenter,
        dispatcher: PresenterOutputDispatcher,
        messenger: RunTelegramMessenger,
        lifecycle_message: Message | None,
    ) -> None:
        self._start = start
        self._task_service = task_service
        self._user_id = user_id
        self._presenter = presenter
        self._dispatcher = dispatcher
        self._messenger = messenger
        self._lifecycle_message = lifecycle_message
        self._interactive_pump: asyncio.Task | None = None

    async def pump_structured_reply(self) -> None:
        try:
            while True:
                changed = await self._presenter.wait_for_update(timeout_sec=0.05)
                if not changed:
                    continue
                await self._dispatcher.emit_presenter_messages(log_missing=False)
        except asyncio.CancelledError:
            raise

    async def stream_events(self) -> None:
        saw_exit = False
        try:
            async for event in self._start.events:
                if event.type in {EventType.STDOUT, EventType.STDERR}:
                    if not event.content:
                        continue
                    if self._start.interactive and self._presenter.structured_session_available:
                        continue
                    logger.info(
                        "[task %s][%s] %s",
                        self._start.task.task_id,
                        event.type.value,
                        event.content.rstrip("\n"),
                    )
                    prefix = "" if event.type == EventType.STDOUT else "[stderr] "
                    await self._dispatcher.push_text(f"{prefix}{event.content}")
                    continue

                if event.type == EventType.STARTED:
                    logger.info(
                        "task stream started task_id=%s provider=%s user_id=%s",
                        self._start.task.task_id,
                        self._start.task.provider,
                        self._user_id,
                    )
                    started_message = _build_started_message(task_id=self._start.task.task_id)
                    if not await self._messenger.edit_message_safely(self._lifecycle_message, started_message):
                        await self._messenger.answer_safely(started_message)
                    if self._start.interactive and self._interactive_pump is None:
                        self._interactive_pump = asyncio.create_task(self.pump_structured_reply())
                    continue

                if self._start.interactive:
                    await self._dispatcher.emit_presenter_messages(log_missing=True)
                await self._dispatcher.flush()
                duration, truncated = await _load_status_summary(self._task_service, self._start.task.task_id, self._user_id)

                if event.type == EventType.EXITED:
                    saw_exit = True
                    await self._messenger.answer_safely(
                        _build_success_message(
                            task_id=self._start.task.task_id,
                            exit_code=event.exit_code,
                            duration=duration,
                            truncated=truncated,
                        )
                    )
                elif event.type in {EventType.FAILED, EventType.TIMEOUT, EventType.CANCELED}:
                    error_text = normalize_stream_text(event.error or "") or "-"
                    logger.error(
                        "task event error",
                        extra={
                            "task_id": self._start.task.task_id,
                            "user_id": self._user_id,
                            "provider": self._start.task.provider,
                            "event_type": event.type.value,
                            "error": error_text,
                            "duration": duration,
                        },
                    )
                    await self._messenger.answer_safely(
                        _build_error_message(
                            event_type=event.type,
                            task_id=self._start.task.task_id,
                            error_text=error_text,
                            duration=duration,
                            truncated=truncated,
                        )
                    )
        finally:
            if saw_exit and self._start.interactive:
                await asyncio.sleep(0.1)
                await self._dispatcher.emit_presenter_messages(final=True, log_missing=True)
                await self._dispatcher.flush()
            if self._interactive_pump is not None:
                self._interactive_pump.cancel()
                try:
                    await self._interactive_pump
                except asyncio.CancelledError:
                    pass
