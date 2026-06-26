from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.command_utils import split_command_text
from app.bot.handlers.run_event_streamer import (
    _SPINNER_INITIAL_DELAY_SEC,
    _SPINNER_INTERVAL_SEC,
    _STRUCTURED_REPLY_PUMP_INTERVAL_SEC,
    RunEventStreamer,
    _build_created_message,
)
from app.bot.handlers.run_presenter_dispatcher import PresenterOutputDispatcher
from app.bot.handlers.run_telegram_messenger import RunTelegramMessenger
from app.bot.handlers.user_utils import extract_user_id
from app.bot.presenters.chunk_sender import ChunkSender
from app.bot.presenters.structured_reply_presenter import (
    _MARKER_LINE_RE as _PRESENTER_MARKER_LINE_RE,
)
from app.bot.presenters.structured_reply_presenter import (
    StructuredReplyPresenter,
)
from app.bot.presenters.tool_message_manager import ToolMessageManager
from app.domain.models import EventType
from app.services.diff_generator import DiffGeneratorService
from app.services.result_exporter import ResultExporterService
from app.services.status_display import StatusDisplayService
from app.services.task_service import TaskService

if TYPE_CHECKING:
    from app.services.permission_gateway import PermissionGateway

logger = logging.getLogger(__name__)

_MARKER_LINE_RE = _PRESENTER_MARKER_LINE_RE
_ACTIVE_STREAM_TASKS: set[asyncio.Task] = set()
_ABANDONED_STREAM_TASKS: set[asyncio.Task] = set()
_STREAM_WATCHDOG_BUFFER_SEC = 30.0
_STREAM_WATCHDOG_MIN_SEC = 1.0
_STREAM_WATCHDOG_CHECK_INTERVAL_SEC = 0.5
_STREAM_WATCHDOG_FINALIZE_GRACE_SEC = 30.0
_STREAM_WATCHDOG_CANCEL_GRACE_SEC = 5.0


def _stream_watchdog_timeout(timeout_sec: int | float | None) -> float:
    if timeout_sec is None:
        return _STREAM_WATCHDOG_BUFFER_SEC
    return max(float(timeout_sec) + _STREAM_WATCHDOG_BUFFER_SEC, _STREAM_WATCHDOG_MIN_SEC)


def parse_run_args(text: str | None) -> tuple[str | None, str]:
    if not text:
        raise ValueError("用法: /run <provider> <task text>")

    parts = split_command_text(text, maxsplit=1)
    if len(parts) < 2:
        raise ValueError("用法: /run <provider> <task text>")

    provider = parts[0].strip().lower()
    prompt = parts[1].strip()
    if not prompt:
        raise ValueError("task text 不能为空")
    return provider, prompt


@dataclass
class _StreamingComponents:
    """Bundled streaming components created for a run task."""

    messenger: RunTelegramMessenger
    lifecycle_message: Message | None
    sender: ChunkSender
    presenter: StructuredReplyPresenter
    tool_message_manager: ToolMessageManager
    dispatcher: PresenterOutputDispatcher
    streamer: RunEventStreamer


async def _create_streaming_components(
    *,
    message: Message,
    task_service: TaskService,
    sender_factory: Callable[[], ChunkSender],
    user_id: int,
    start: Any,
    permission_gateway: PermissionGateway | None,
    diff_generator: DiffGeneratorService | None,
    result_exporter: ResultExporterService | None,
    status_display: StatusDisplayService | None,
    schedule_uploads_fn: Callable[[], None] | None,
    structured_reply_pump_interval_sec: float,
    spinner_initial_delay_sec: float,
    spinner_interval_sec: float,
) -> _StreamingComponents:
    """Create and wire all streaming components for a task run."""
    messenger = RunTelegramMessenger(
        root_message=message,
        task_id=start.task.task_id,
        user_id=user_id,
        provider=start.task.provider,
    )
    lifecycle_message = await messenger.send_message_safely(
        _build_created_message(
            task_id=start.task.task_id,
            provider=start.task.provider,
            session_id=start.task.session_id,
        )
    )
    await messenger.set_reaction("⚡")

    sender: ChunkSender = sender_factory()
    presenter = StructuredReplyPresenter(
        task_service=task_service,
        user_id=user_id,
        task_id=start.task.task_id,
        task_started_at=start.task.started_at or start.task.created_at,
    )
    tool_message_manager = ToolMessageManager(
        root_message=message,
        task_id=start.task.task_id,
        user_id=user_id,
        provider=start.task.provider,
    )
    dispatcher = PresenterOutputDispatcher(
        presenter=presenter,
        sender=sender,
        messenger=messenger,
        tool_message_manager=tool_message_manager,
        task_id=start.task.task_id,
        permission_gateway=permission_gateway,
    )
    streamer = RunEventStreamer(
        start=start,
        task_service=task_service,
        user_id=user_id,
        presenter=presenter,
        dispatcher=dispatcher,
        messenger=messenger,
        lifecycle_message=lifecycle_message,
        diff_generator=diff_generator,
        result_exporter=result_exporter,
        queued_upload_scheduler=schedule_uploads_fn,
        status_display=status_display,
        structured_reply_pump_interval_sec=structured_reply_pump_interval_sec,
        spinner_initial_delay_sec=spinner_initial_delay_sec,
        spinner_interval_sec=spinner_interval_sec,
    )
    await presenter.prime(baseline_current_snapshot=True)

    return _StreamingComponents(
        messenger=messenger,
        lifecycle_message=lifecycle_message,
        sender=sender,
        presenter=presenter,
        tool_message_manager=tool_message_manager,
        dispatcher=dispatcher,
        streamer=streamer,
    )


def _wrap_events_with_progress(
    original_events: Any,
    state: _WatchdogState,
) -> Any:
    """Wrap event stream to track progress timestamps and terminal events."""

    async def _events_with_progress():
        async for event in original_events:
            if state.stream_abandoned:
                return
            now = asyncio.get_running_loop().time()
            state.last_progress_at = now
            if event.type in {EventType.EXITED, EventType.FAILED, EventType.TIMEOUT, EventType.CANCELED}:
                state.terminal_seen = True
                state.terminal_seen_at = now
            yield event

    return _events_with_progress()


@dataclass
class _WatchdogState:
    """Mutable state shared between the progress wrapper and the watchdog loop."""

    last_progress_at: float
    terminal_seen: bool = False
    terminal_seen_at: float | None = None
    stream_abandoned: bool = False
    queued_upload_scheduled: bool = False


class _StreamWatchdog:
    """Monitors a stream task and enforces timeout / finalization deadlines."""

    def __init__(
        self,
        *,
        start: Any,
        task_service: TaskService,
        user_id: int,
        messenger: RunTelegramMessenger,
        streamer: RunEventStreamer,
        state: _WatchdogState,
        schedule_uploads: Callable[[], None],
        message: Message,
    ) -> None:
        self._start = start
        self._task_service = task_service
        self._user_id = user_id
        self._messenger = messenger
        self._streamer = streamer
        self._state = state
        self._schedule_uploads = schedule_uploads
        self._message = message

    async def run(self) -> None:
        """Run the stream task with watchdog monitoring."""
        timeout = _stream_watchdog_timeout(getattr(self._start.task, "timeout_sec", None))
        last_structured_cursor: int | None = None
        stream_task = asyncio.create_task(self._streamer.stream_events())

        try:
            while True:
                done, _ = await asyncio.wait(
                    {stream_task},
                    timeout=min(_STREAM_WATCHDOG_CHECK_INTERVAL_SEC, timeout),
                )
                if stream_task in done:
                    await stream_task
                    return

                now = asyncio.get_running_loop().time()
                if self._start.interactive:
                    last_structured_cursor = await self._check_interactive_cursor(
                        last_structured_cursor,
                        now,
                    )

                if self._state.terminal_seen:
                    result = await self._handle_terminal_grace(stream_task, now)
                    if result is not None:
                        return
                    # Still in terminal grace period – skip idle timeout check.
                    continue

                idle_sec = now - self._state.last_progress_at
                if idle_sec < timeout:
                    continue
                stopped = await self._handle_idle_timeout(stream_task, timeout)
                if stopped:
                    return
        finally:
            if not stream_task.done():
                await self._cancel_stream_task(stream_task)

    async def _check_interactive_cursor(
        self,
        last_cursor: int | None,
        now: float,
    ) -> int | None:
        """Check structured session cursor for interactive progress."""
        cursor = await self._task_service.get_structured_session_cursor(
            self._user_id,
            task_id=self._start.task.task_id,
        )
        if last_cursor is None:
            return cursor
        if cursor != last_cursor:
            self._state.last_progress_at = now
            return cursor
        return last_cursor

    async def _handle_terminal_grace(
        self,
        stream_task: asyncio.Task,
        now: float,
    ) -> bool | None:
        """Handle the grace period after a terminal event is seen. Returns True if abandoned."""
        terminal_seen_at = self._state.terminal_seen_at or now
        if now - terminal_seen_at < _STREAM_WATCHDOG_FINALIZE_GRACE_SEC:
            return None
        self._state.stream_abandoned = True
        await self._cancel_stream_task(stream_task)
        force_cleanup = getattr(self._streamer, "force_cleanup", None)
        if force_cleanup is not None:
            await force_cleanup(schedule_uploads=True, cancel_timeout_sec=_STREAM_WATCHDOG_CANCEL_GRACE_SEC)
        else:
            self._schedule_uploads()
        logger.error(
            "task stream finalization watchdog timeout",
            extra={
                "task_id": self._start.task.task_id,
                "user_id": self._user_id,
                "timeout_sec": _STREAM_WATCHDOG_FINALIZE_GRACE_SEC,
            },
        )
        await self._messenger.answer_safely("任务收尾处理超时，已停止后台监听。")
        return True

    async def _handle_idle_timeout(
        self,
        stream_task: asyncio.Task,
        timeout: float,
    ) -> bool:
        """Handle idle timeout. Returns True if the stream was abandoned."""
        reason = "任务流处理超时"
        marked = await self._mark_stream_timeout(reason)
        if not marked:
            self._state.last_progress_at = asyncio.get_running_loop().time()
            return False
        self._state.stream_abandoned = True
        await self._cancel_stream_task(stream_task)
        self._schedule_uploads()
        logger.error(
            "task stream watchdog timeout",
            extra={
                "task_id": self._start.task.task_id,
                "user_id": self._user_id,
                "timeout_sec": timeout,
            },
        )
        await self._messenger.answer_safely("任务流处理超时，已停止后台监听。")
        return True

    async def _cancel_stream_task(self, stream_task: asyncio.Task) -> None:
        """Cancel the stream task with a grace period."""
        if stream_task.done():
            _consume_task_result(stream_task)
            return
        if stream_task in _ABANDONED_STREAM_TASKS:
            return
        stream_task.cancel()
        done, _ = await asyncio.wait({stream_task}, timeout=_STREAM_WATCHDOG_CANCEL_GRACE_SEC)
        if stream_task in done:
            with suppress(asyncio.CancelledError):
                await stream_task
            return
        _ABANDONED_STREAM_TASKS.add(stream_task)
        stream_task.add_done_callback(_forget_abandoned_stream_task)
        logger.error(
            "task stream cancellation grace timeout",
            extra={
                "task_id": self._start.task.task_id,
                "user_id": self._user_id,
                "timeout_sec": _STREAM_WATCHDOG_CANCEL_GRACE_SEC,
            },
        )

    async def _mark_stream_timeout(self, reason: str) -> bool:
        """Mark the stream as timed out and cancel the underlying task."""
        mark_and_cancel = getattr(self._task_service, "mark_stream_timeout_and_cancel", None)
        if mark_and_cancel is not None:
            marked, _ = await mark_and_cancel(
                self._start.task.task_id,
                self._user_id,
                reason=reason,
                cancel_timeout_sec=_STREAM_WATCHDOG_CANCEL_GRACE_SEC,
            )
            return bool(marked)

        marked = True
        mark_stream_timeout = getattr(self._task_service, "mark_stream_timeout", None)
        if mark_stream_timeout is not None:
            marked = bool(await mark_stream_timeout(self._start.task.task_id, self._user_id, reason=reason))
        if marked:
            cancel = getattr(self._task_service, "cancel", None)
            if cancel is not None:
                await cancel(self._start.task.task_id, self._user_id)
        return marked


def _consume_task_result(done_task: asyncio.Task) -> None:
    """Consume a done task's result/exception to suppress unhandled warnings."""
    if done_task.cancelled():
        return
    with suppress(Exception):
        done_task.exception()


def _forget_abandoned_stream_task(done_task: asyncio.Task) -> None:
    """Callback for abandoned stream tasks to prevent leaks."""
    _ABANDONED_STREAM_TASKS.discard(done_task)
    _consume_task_result(done_task)


def _on_stream_done(
    done_task: asyncio.Task,
    *,
    task_id: str,
    user_id: int,
    provider: str,
    messenger: RunTelegramMessenger,
) -> None:
    """Done callback for the outer stream watchdog task."""
    _ACTIVE_STREAM_TASKS.discard(done_task)
    if done_task.cancelled():
        return
    exc = done_task.exception()
    if exc is None:
        return
    logger.error(
        "task stream exception",
        extra={
            "task_id": task_id,
            "user_id": user_id,
            "provider": provider,
            "error": str(exc),
        },
        exc_info=(type(exc), exc, exc.__traceback__),
    )

    async def _notify_error() -> None:
        try:
            await messenger.answer_safely(f"任务处理异常: {exc}")
        except Exception:
            logger.exception(
                "failed to notify user about task error",
                extra={"task_id": task_id, "user_id": user_id},
            )

    notify_task = asyncio.get_running_loop().create_task(_notify_error())

    def _log_notify_error(done: asyncio.Task[None]) -> None:
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            logger.error("error notification task failed", exc_info=(type(exc), exc, exc.__traceback__))

    notify_task.add_done_callback(_log_notify_error)


async def run_prompt_and_stream(
    *,
    message: Message,
    task_service: TaskService,
    sender_factory,
    user_id: int,
    provider: str | None,
    prompt: str,
    workdir: str | None = None,
    diff_generator: DiffGeneratorService | None = None,
    result_exporter: ResultExporterService | None = None,
    status_display: StatusDisplayService | None = None,
    queued_upload_scheduler: Callable[[Message, int, str], None] | None = None,
    pending_upload_finalizer: Callable[[Message, int], Awaitable[None]] | None = None,
    permission_gateway: PermissionGateway | None = None,
    structured_reply_pump_interval_sec: float = _STRUCTURED_REPLY_PUMP_INTERVAL_SEC,
    spinner_initial_delay_sec: float = _SPINNER_INITIAL_DELAY_SEC,
    spinner_interval_sec: float = _SPINNER_INTERVAL_SEC,
) -> asyncio.Task | None:
    logger.info(
        "run prompt requested",
        extra={
            "user_id": user_id,
            "provider": provider,
            "prompt_len": len(prompt),
            "workdir": workdir,
        },
    )
    try:
        if pending_upload_finalizer is not None:
            await pending_upload_finalizer(message, user_id)
        start = await task_service.create_and_run(
            user_id=user_id,
            provider=provider,
            prompt=prompt,
            workdir=workdir,
        )
    except ValueError as exc:
        await message.answer(f"参数错误: {exc}")
        return None
    except Exception:
        logger.exception("failed to create task", extra={"user_id": user_id, "provider": provider})
        await message.answer("创建任务失败，请稍后重试")
        return None

    logger.info(
        "run prompt created task",
        extra={
            "user_id": user_id,
            "task_id": start.task.task_id,
            "provider": start.task.provider,
            "session_id": start.task.session_id,
            "interactive": start.interactive,
        },
    )

    loop = asyncio.get_running_loop()
    state = _WatchdogState(last_progress_at=loop.time())

    def _schedule_queued_uploads_once() -> None:
        if state.queued_upload_scheduled or queued_upload_scheduler is None:
            return
        state.queued_upload_scheduled = True
        try:
            queued_upload_scheduler(message, user_id, start.task.task_id)
        except Exception:
            logger.exception("failed to schedule queued upload processing", extra={"user_id": user_id})

    schedule_uploads_fn = _schedule_queued_uploads_once if queued_upload_scheduler is not None else None
    components = await _create_streaming_components(
        message=message,
        task_service=task_service,
        sender_factory=sender_factory,
        user_id=user_id,
        start=start,
        permission_gateway=permission_gateway,
        diff_generator=diff_generator,
        result_exporter=result_exporter,
        status_display=status_display,
        schedule_uploads_fn=schedule_uploads_fn,
        structured_reply_pump_interval_sec=structured_reply_pump_interval_sec,
        spinner_initial_delay_sec=spinner_initial_delay_sec,
        spinner_interval_sec=spinner_interval_sec,
    )

    start.events = _wrap_events_with_progress(start.events, state)

    watchdog = _StreamWatchdog(
        start=start,
        task_service=task_service,
        user_id=user_id,
        messenger=components.messenger,
        streamer=components.streamer,
        state=state,
        schedule_uploads=_schedule_queued_uploads_once,
        message=message,
    )

    task = asyncio.create_task(watchdog.run())
    _ACTIVE_STREAM_TASKS.add(task)
    logger.info(
        "task stream spawned",
        extra={
            "task_id": start.task.task_id,
            "user_id": user_id,
            "interactive": start.interactive,
            "active_stream_tasks": len(_ACTIVE_STREAM_TASKS),
        },
    )

    task.add_done_callback(
        lambda dt: _on_stream_done(
            dt,
            task_id=start.task.task_id,
            user_id=user_id,
            provider=start.task.provider,
            messenger=components.messenger,
        )
    )
    return task


def register_run_handler(
    router,
    *,
    task_service: TaskService,
    sender_factory,
    diff_generator: DiffGeneratorService | None = None,
    result_exporter: ResultExporterService | None = None,
    status_display: StatusDisplayService | None = None,
    queued_upload_scheduler: Callable[[Message, int, str], None] | None = None,
    pending_upload_finalizer: Callable[[Message, int], Awaitable[None]] | None = None,
    permission_gateway: PermissionGateway | None = None,
    structured_reply_pump_interval_sec: float = _STRUCTURED_REPLY_PUMP_INTERVAL_SEC,
    spinner_initial_delay_sec: float = _SPINNER_INITIAL_DELAY_SEC,
    spinner_interval_sec: float = _SPINNER_INTERVAL_SEC,
):
    @router.message(Command("run"))
    async def command_run(message: Message, command: CommandObject) -> None:
        provider, prompt = parse_run_args(command.args)

        user_id = extract_user_id(message)
        await run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=sender_factory,
            user_id=user_id,
            provider=provider,
            prompt=prompt,
            diff_generator=diff_generator,
            result_exporter=result_exporter,
            status_display=status_display,
            queued_upload_scheduler=queued_upload_scheduler,
            pending_upload_finalizer=pending_upload_finalizer,
            permission_gateway=permission_gateway,
            structured_reply_pump_interval_sec=structured_reply_pump_interval_sec,
            spinner_initial_delay_sec=spinner_initial_delay_sec,
            spinner_interval_sec=spinner_interval_sec,
        )
