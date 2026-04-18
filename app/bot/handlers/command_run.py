from __future__ import annotations

import asyncio
import logging

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.presenters.chunk_sender import ChunkSender
from app.bot.presenters.structured_reply_presenter import (
    StructuredReplyPresenter,
    _MARKER_LINE_RE as _PRESENTER_MARKER_LINE_RE,
    normalize_stream_text,
    preview_stream_text,
)
from app.domain.models import EventType
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

_MARKER_LINE_RE = _PRESENTER_MARKER_LINE_RE
_ACTIVE_STREAM_TASKS: set[asyncio.Task] = set()


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


async def run_prompt_and_stream(
    *,
    message: Message,
    task_service: TaskService,
    sender_factory,
    user_id: int,
    provider: str | None,
    prompt: str,
    workdir: str | None = None,
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
        return
    except Exception as exc:
        logger.exception(
            "task create failed",
            extra={"user_id": user_id, "provider": provider},
        )
        await message.answer(f"创建任务失败: {exc}")
        return

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
    async def answer_safely(text: str) -> bool:
        if not text:
            return False
        try:
            await message.answer(text)
            return True
        except Exception:
            logger.exception(
                "telegram answer failed",
                extra={"task_id": start.task.task_id, "user_id": user_id, "provider": start.task.provider},
            )
            return False

    await answer_safely(
        _build_created_message(
            task_id=start.task.task_id,
            provider=start.task.provider,
            session_id=start.task.session_id,
        )
    )

    sender: ChunkSender = sender_factory()
    presenter = StructuredReplyPresenter(task_service=task_service, user_id=user_id)
    await presenter.prime(baseline_current_snapshot=True)
    interactive_pump: asyncio.Task | None = None

    async def send_text(text: str) -> None:
        preview = preview_stream_text(text)
        if not preview:
            return
        await answer_safely(preview)

    async def emit_presenter_messages(*, final: bool = False, log_missing: bool) -> None:
        for output in await presenter.poll(task_id=start.task.task_id, final=final, log_missing=log_missing):
            await sender.push(output, send_text)

    async def pump_structured_reply() -> None:
        try:
            while True:
                changed = await presenter.wait_for_update(timeout_sec=0.05)
                if not changed:
                    continue
                await emit_presenter_messages(log_missing=False)
        except asyncio.CancelledError:
            raise

    async def stream_events() -> None:
        nonlocal interactive_pump
        saw_exit = False
        try:
            async for event in start.events:
                if event.type in {EventType.STDOUT, EventType.STDERR}:
                    if not event.content:
                        continue
                    if start.interactive and presenter.structured_session_available:
                        continue
                    logger.info(
                        "[task %s][%s] %s",
                        start.task.task_id,
                        event.type.value,
                        event.content.rstrip("\n"),
                    )
                    prefix = "" if event.type == EventType.STDOUT else "[stderr] "
                    await sender.push(f"{prefix}{event.content}", send_text)
                    continue

                if event.type == EventType.STARTED:
                    logger.info(
                        "task stream started task_id=%s provider=%s user_id=%s",
                        start.task.task_id,
                        start.task.provider,
                        user_id,
                    )
                    await answer_safely(_build_started_message(task_id=start.task.task_id))
                    if start.interactive and interactive_pump is None:
                        interactive_pump = asyncio.create_task(pump_structured_reply())
                    continue

                if start.interactive:
                    await emit_presenter_messages(log_missing=True)
                await sender.flush(send_text)
                duration, truncated = await _load_status_summary(task_service, start.task.task_id, user_id)

                if event.type == EventType.EXITED:
                    saw_exit = True
                    await answer_safely(
                        _build_success_message(
                            task_id=start.task.task_id,
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
                            "task_id": start.task.task_id,
                            "user_id": user_id,
                            "provider": start.task.provider,
                            "event_type": event.type.value,
                            "error": error_text,
                            "duration": duration,
                        },
                    )
                    await answer_safely(
                        _build_error_message(
                            event_type=event.type,
                            task_id=start.task.task_id,
                            error_text=error_text,
                            duration=duration,
                            truncated=truncated,
                        )
                    )
        finally:
            if saw_exit and start.interactive:
                await asyncio.sleep(0.1)
                await emit_presenter_messages(final=True, log_missing=True)
                await sender.flush(send_text)
            if interactive_pump is not None:
                interactive_pump.cancel()
                try:
                    await interactive_pump
                except asyncio.CancelledError:
                    pass

    task = asyncio.create_task(stream_events())
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
            await answer_safely(f"任务处理异常: {exc}")

        asyncio.get_running_loop().create_task(_notify_error())

    task.add_done_callback(_on_done)
    return task


def register_run_handler(router, *, task_service: TaskService, sender_factory):
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
        )
