from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from contextlib import suppress

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.handlers.run_event_streamer import RunEventStreamer, _build_created_message
from app.bot.handlers.run_presenter_dispatcher import PresenterOutputDispatcher
from app.bot.handlers.run_telegram_messenger import RunTelegramMessenger
from app.bot.presenters.chunk_sender import ChunkSender
from app.bot.presenters.structured_reply_presenter import (
    StructuredReplyPresenter,
    _MARKER_LINE_RE as _PRESENTER_MARKER_LINE_RE,
)
from app.bot.presenters.tool_message_manager import ToolMessageManager
from app.domain.models import EventType
from app.services.diff_generator import DiffGeneratorService
from app.services.result_exporter import ResultExporterService
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

    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        raise ValueError("用法: /run <provider> <task text>")

    provider = parts[0].strip().lower()
    prompt = parts[1].strip()
    if not prompt:
        raise ValueError("task text 不能为空")
    return provider, prompt


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
    queued_upload_scheduler: Callable[[Message, int, str], None] | None = None,
    permission_gateway: PermissionGateway | None = None,
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
        start = await task_service.create_and_run(
            user_id=user_id,
            provider=provider,
            prompt=prompt,
            workdir=workdir,
        )
    except ValueError as exc:
        logger.warning(
            "task create validation failed",
            extra={"user_id": user_id, "provider": provider, "error": str(exc)},
        )
        await message.answer(f"参数错误: {exc}")
        return None
    except Exception as exc:
        logger.exception(
            "task create failed",
            extra={"user_id": user_id, "provider": provider},
        )
        await message.answer(f"创建任务失败: {exc}")
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
    loop = asyncio.get_running_loop()
    last_stream_progress_at = loop.time()
    stream_terminal_seen = False
    stream_terminal_seen_at: float | None = None
    stream_abandoned = False
    queued_upload_scheduled = False
    original_events = start.events

    def _schedule_queued_uploads_once() -> None:
        nonlocal queued_upload_scheduled
        if queued_upload_scheduler is None or queued_upload_scheduled:
            return
        queued_upload_scheduled = True
        try:
            queued_upload_scheduler(message, user_id, start.task.task_id)
        except Exception:
            logger.exception("failed to schedule queued upload processing", extra={"user_id": user_id})

    async def _events_with_progress():
        nonlocal last_stream_progress_at, stream_terminal_seen, stream_terminal_seen_at
        async for event in original_events:
            if stream_abandoned:
                return
            now = asyncio.get_running_loop().time()
            last_stream_progress_at = now
            if event.type in {EventType.EXITED, EventType.FAILED, EventType.TIMEOUT, EventType.CANCELED}:
                stream_terminal_seen = True
                stream_terminal_seen_at = now
            yield event

    start.events = _events_with_progress()
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
        queued_upload_scheduler=_schedule_queued_uploads_once if queued_upload_scheduler is not None else None,
    )
    await presenter.prime(baseline_current_snapshot=True)

    async def _run_stream_with_watchdog() -> None:
        nonlocal last_stream_progress_at, stream_abandoned

        timeout = _stream_watchdog_timeout(getattr(start.task, "timeout_sec", None))
        last_structured_cursor: int | None = None
        stream_task = asyncio.create_task(streamer.stream_events())

        def _consume_stream_task_result(done_task: asyncio.Task) -> None:
            if done_task.cancelled():
                return
            with suppress(Exception):
                done_task.exception()

        def _forget_abandoned_stream_task(done_task: asyncio.Task) -> None:
            _ABANDONED_STREAM_TASKS.discard(done_task)
            _consume_stream_task_result(done_task)

        async def _cancel_stream_task() -> None:
            if stream_task.done():
                _consume_stream_task_result(stream_task)
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
                    "task_id": start.task.task_id,
                    "user_id": user_id,
                    "timeout_sec": _STREAM_WATCHDOG_CANCEL_GRACE_SEC,
                },
            )

        async def _mark_stream_timeout(reason: str) -> bool:
            mark_and_cancel = getattr(task_service, "mark_stream_timeout_and_cancel", None)
            if mark_and_cancel is not None:
                marked, _ = await mark_and_cancel(
                    start.task.task_id,
                    user_id,
                    reason=reason,
                    cancel_timeout_sec=_STREAM_WATCHDOG_CANCEL_GRACE_SEC,
                )
                return bool(marked)

            marked = True
            mark_stream_timeout = getattr(task_service, "mark_stream_timeout", None)
            if mark_stream_timeout is not None:
                marked = bool(await mark_stream_timeout(start.task.task_id, user_id, reason=reason))
            if marked:
                cancel = getattr(task_service, "cancel", None)
                if cancel is not None:
                    await cancel(start.task.task_id, user_id)
            return marked

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
                if start.interactive:
                    cursor = await task_service.get_structured_session_cursor(user_id, task_id=start.task.task_id)
                    if last_structured_cursor is None:
                        last_structured_cursor = cursor
                    elif cursor != last_structured_cursor:
                        last_structured_cursor = cursor
                        last_stream_progress_at = now

                if stream_terminal_seen:
                    terminal_seen_at = stream_terminal_seen_at or now
                    if now - terminal_seen_at < _STREAM_WATCHDOG_FINALIZE_GRACE_SEC:
                        continue
                    stream_abandoned = True
                    await _cancel_stream_task()
                    force_cleanup = getattr(streamer, "force_cleanup", None)
                    if force_cleanup is not None:
                        await force_cleanup(schedule_uploads=True, cancel_timeout_sec=_STREAM_WATCHDOG_CANCEL_GRACE_SEC)
                    else:
                        _schedule_queued_uploads_once()
                    logger.error(
                        "task stream finalization watchdog timeout",
                        extra={
                            "task_id": start.task.task_id,
                            "user_id": user_id,
                            "timeout_sec": _STREAM_WATCHDOG_FINALIZE_GRACE_SEC,
                        },
                    )
                    await messenger.answer_safely("任务收尾处理超时，已停止后台监听。")
                    return

                idle_sec = now - last_stream_progress_at
                if idle_sec < timeout:
                    continue
                reason = "任务流处理超时"
                marked = await _mark_stream_timeout(reason)
                if not marked:
                    last_stream_progress_at = now
                    continue
                stream_abandoned = True
                await _cancel_stream_task()
                _schedule_queued_uploads_once()
                logger.error(
                    "task stream watchdog timeout",
                    extra={"task_id": start.task.task_id, "user_id": user_id, "timeout_sec": timeout},
                )
                await messenger.answer_safely("任务流处理超时，已停止后台监听。")
                return
        finally:
            if not stream_task.done():
                await _cancel_stream_task()

    task = asyncio.create_task(_run_stream_with_watchdog())
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

    def _on_done(done_task: asyncio.Task) -> None:
        _ACTIVE_STREAM_TASKS.discard(done_task)
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is None:
            return
        logger.error(
            "task stream exception",
            extra={
                "task_id": start.task.task_id,
                "user_id": user_id,
                "provider": start.task.provider,
                "error": str(exc),
            },
            exc_info=(type(exc), exc, exc.__traceback__),
        )

        async def _notify_error() -> None:
            await messenger.answer_safely(f"任务处理异常: {exc}")

        asyncio.get_running_loop().create_task(_notify_error())

    task.add_done_callback(_on_done)
    return task


def register_run_handler(
    router,
    *,
    task_service: TaskService,
    sender_factory,
    diff_generator: DiffGeneratorService | None = None,
    result_exporter: ResultExporterService | None = None,
    queued_upload_scheduler: Callable[[Message, int, str], None] | None = None,
    permission_gateway: PermissionGateway | None = None,
):
    @router.message(Command("run"))
    async def command_run(message: Message, command: CommandObject) -> None:
        try:
            provider, prompt = parse_run_args(command.args)
        except ValueError as exc:
            await message.answer(str(exc))
            return

        user_id = message.from_user.id if message.from_user else 0
        await run_prompt_and_stream(
            message=message,
            task_service=task_service,
            sender_factory=sender_factory,
            user_id=user_id,
            provider=provider,
            prompt=prompt,
            diff_generator=diff_generator,
            result_exporter=result_exporter,
            queued_upload_scheduler=queued_upload_scheduler,
            permission_gateway=permission_gateway,
        )
