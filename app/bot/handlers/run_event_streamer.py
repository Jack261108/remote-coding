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
    short_id = task_id[:8]
    return f"⏳ 处理中… [{short_id}]"


def _build_success_message(*, task_id: str, exit_code: int | None, duration: str, truncated: bool) -> str:
    short_id = task_id[:8]
    parts = [f"✅ 完成 [{short_id}] {duration}"]
    if truncated:
        parts.append("（输出已截断）")
    return " ".join(parts)


def _build_error_message(*, event_type: EventType, task_id: str, error_text: str, duration: str, truncated: bool) -> str:
    short_id = task_id[:8]
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
    icon = icon_map.get(event_type, "❌")
    label = label_map.get(event_type, "错误")
    parts = [f"{icon} {label} [{short_id}] {duration}"]
    if error_text and error_text != "-":
        parts.append(f"\n{error_text}")
    if truncated:
        parts.append("（输出已截断）")
    return "".join(parts)


_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_INTERVAL_SEC = 1.0
_SPINNER_INITIAL_DELAY_SEC = 3.0


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
        self._spinner_task: asyncio.Task | None = None
        self._emit_lock = asyncio.Lock()

    def _start_spinner(self) -> None:
        if self._lifecycle_message is None:
            return
        if self._spinner_task is not None and not self._spinner_task.done():
            return
        self._spinner_task = asyncio.create_task(self._spin())

    async def _stop_spinner(self) -> None:
        task = self._spinner_task
        self._spinner_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _spin(self) -> None:
        short_id = self._start.task.task_id[:8]
        frame_idx = 0
        try:
            # Skip animation for short tasks: wait before the first frame.
            await asyncio.sleep(_SPINNER_INITIAL_DELAY_SEC)
            while True:
                frame = _SPINNER_FRAMES[frame_idx % len(_SPINNER_FRAMES)]
                frame_idx += 1
                text = f"{frame} 处理中… [{short_id}]"
                await self._messenger.edit_message_safely(self._lifecycle_message, text)
                await asyncio.sleep(_SPINNER_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise

    async def pump_structured_reply(self) -> None:
        try:
            while True:
                changed = await self._presenter.wait_for_update(timeout_sec=0.05)
                if not changed:
                    continue
                async with self._emit_lock:
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
                    if self._start.interactive:
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
                    self._start_spinner()
                    if self._start.interactive and self._interactive_pump is None:
                        self._interactive_pump = asyncio.create_task(self.pump_structured_reply())
                    continue

                if self._start.interactive:
                    async with self._emit_lock:
                        await self._dispatcher.emit_presenter_messages(log_missing=True)
                await self._dispatcher.flush()
                await self._stop_spinner()
                duration, truncated = await _load_status_summary(self._task_service, self._start.task.task_id, self._user_id)

                if event.type == EventType.EXITED:
                    saw_exit = True
                    success_msg = _build_success_message(
                        task_id=self._start.task.task_id,
                        exit_code=event.exit_code,
                        duration=duration,
                        truncated=truncated,
                    )
                    if not await self._messenger.edit_message_safely(self._lifecycle_message, success_msg):
                        await self._messenger.answer_safely(success_msg)
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
                    error_msg = _build_error_message(
                        event_type=event.type,
                        task_id=self._start.task.task_id,
                        error_text=error_text,
                        duration=duration,
                        truncated=truncated,
                    )
                    if not await self._messenger.edit_message_safely(self._lifecycle_message, error_msg):
                        await self._messenger.answer_safely(error_msg)
        finally:
            await self._stop_spinner()
            if saw_exit and self._start.interactive:
                await asyncio.sleep(0.1)
                # Freeze the presenter's last turn ID to prevent emitting
                # new turns that arrive after task completion (e.g., idle greetings).
                self._presenter.freeze_reply_cursor()
                async with self._emit_lock:
                    await self._dispatcher.emit_presenter_messages(final=True, log_missing=True)
                await self._dispatcher.flush()
            if self._interactive_pump is not None:
                self._interactive_pump.cancel()
                try:
                    await self._interactive_pump
                except asyncio.CancelledError:
                    pass
