from __future__ import annotations

import asyncio
import logging
import re

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.presenters.chunk_sender import ChunkSender
from app.domain.models import EventType
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

_MARKER_LINE_RE = re.compile(r"^\s*_*(?:TGCLI_BEGIN|TGCLI_DONE)_*(?:\s*[:：]?\s*[A-Za-z0-9_-]+)?\s*$", re.IGNORECASE)
_BLANK_LINE_BURST_RE = re.compile(r"\n{3,}")
_STREAM_PREVIEW_CHAR_LIMIT = 1800
_STREAM_PREVIEW_LINE_LIMIT = 60


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


def _strip_bridge_markers(text: str) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    kept: list[str] = []
    for raw_line in lines:
        if _MARKER_LINE_RE.match(raw_line):
            continue
        kept.append(raw_line)
    return "\n".join(kept)


def _normalize_stream_text(text: str) -> str:
    cleaned = _strip_bridge_markers(text).replace("\r\n", "\n").replace("\r", "\n")
    if not cleaned.strip():
        return ""

    normalized_lines = [line.rstrip() for line in cleaned.split("\n")]
    normalized = "\n".join(normalized_lines).strip("\n")
    normalized = _BLANK_LINE_BURST_RE.sub("\n\n", normalized)
    return normalized.strip()


def _preview_stream_text(text: str) -> str:
    normalized = _normalize_stream_text(text)
    if not normalized:
        return ""

    lines = normalized.split("\n")
    needs_line_truncation = len(lines) > _STREAM_PREVIEW_LINE_LIMIT
    preview_lines = lines[:_STREAM_PREVIEW_LINE_LIMIT]
    preview = "\n".join(preview_lines)

    needs_char_truncation = len(preview) > _STREAM_PREVIEW_CHAR_LIMIT
    if needs_char_truncation:
        preview = preview[:_STREAM_PREVIEW_CHAR_LIMIT].rstrip()

    if needs_line_truncation or needs_char_truncation:
        preview = f"{preview}\n...[输出片段过长，已截断本条消息]"

    return preview


async def _load_status_summary(task_service: TaskService, task_id: str, user_id: int) -> tuple[str, bool]:
    status = await task_service.get_status(task_id, user_id)
    duration = f"{status.duration_sec:.2f}s" if status and status.duration_sec is not None else "-"
    truncated = bool(status and status.output_truncated)
    return duration, truncated


async def _load_structured_reply(task_service: TaskService, user_id: int) -> tuple[str | None, str]:
    session = await task_service.get_structured_session(user_id)
    if session is None or not session.turns:
        return None, ""
    for turn in reversed(session.turns):
        if turn.role != "assistant" or not turn.is_complete:
            continue
        return turn.turn_id, _preview_stream_text(turn.text)
    return None, ""


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

    await message.answer(
        _build_created_message(
            task_id=start.task.task_id,
            provider=start.task.provider,
            session_id=start.task.session_id,
        )
    )

    sender: ChunkSender = sender_factory()
    last_structured_turn_id, _ = await _load_structured_reply(task_service, user_id)
    interactive_pump: asyncio.Task | None = None

    async def send_text(text: str) -> None:
        preview = _preview_stream_text(text)
        if not preview:
            return
        await message.answer(preview)

    async def emit_structured_reply() -> None:
        nonlocal last_structured_turn_id
        turn_id, structured_reply = await _load_structured_reply(task_service, user_id)
        if not turn_id or not structured_reply or turn_id == last_structured_turn_id:
            return
        last_structured_turn_id = turn_id
        logger.info("[task %s][structured] %s", start.task.task_id, structured_reply.rstrip("\n"))
        await sender.push(structured_reply, send_text)

    async def pump_structured_reply() -> None:
        try:
            while True:
                await emit_structured_reply()
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise

    async def stream_events() -> None:
        nonlocal interactive_pump
        saw_exit = False
        try:
            async for event in start.events:
                if event.type in {EventType.STDOUT, EventType.STDERR}:
                    if start.interactive:
                        continue
                    if not event.content:
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
                    await message.answer(_build_started_message(task_id=start.task.task_id))
                    if start.interactive and interactive_pump is None:
                        interactive_pump = asyncio.create_task(pump_structured_reply())
                    continue

                if start.interactive:
                    await emit_structured_reply()
                await sender.flush(send_text)
                duration, truncated = await _load_status_summary(task_service, start.task.task_id, user_id)

                if event.type == EventType.EXITED:
                    saw_exit = True
                    await message.answer(
                        _build_success_message(
                            task_id=start.task.task_id,
                            exit_code=event.exit_code,
                            duration=duration,
                            truncated=truncated,
                        )
                    )
                elif event.type in {EventType.FAILED, EventType.TIMEOUT, EventType.CANCELED}:
                    error_text = _normalize_stream_text(event.error or "") or "-"
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
                    await message.answer(
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
                await emit_structured_reply()
                await sender.flush(send_text)
            if interactive_pump is not None:
                interactive_pump.cancel()
                try:
                    await interactive_pump
                except asyncio.CancelledError:
                    pass

    task = asyncio.create_task(stream_events())

    def _on_done(done_task: asyncio.Task) -> None:
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
            await message.answer(f"任务处理异常: {exc}")

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
