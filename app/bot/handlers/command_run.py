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
) -> None:
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

    tmux_mode = task_service.is_claude_tmux_enabled() and start.task.provider == "claude_code"
    await message.answer(
        f"任务已创建\n"
        f"task_id: {start.task.task_id}\n"
        f"provider: {start.task.provider}\n"
        f"session_id: {start.task.session_id}\n"
        f"terminal_mode: {tmux_mode}"
    )

    sender: ChunkSender = sender_factory()

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

    async def send_text(text: str) -> None:
        cleaned = _strip_bridge_markers(text)
        if not cleaned or not cleaned.strip():
            return
        await message.answer(cleaned)

    async def stream_events() -> None:
        async for event in start.events:
            if event.type in {EventType.STDOUT, EventType.STDERR}:
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
                extra = (
                    f"\ntmux_session: {event.content.split('=', 1)[1]}"
                    if event.content and event.content.startswith("tmux_session=")
                    else ""
                )
                await message.answer(f"任务开始执行: {start.task.task_id}{extra}")
                continue

            await sender.flush(send_text)
            if event.type == EventType.EXITED:
                status = await task_service.get_status(start.task.task_id, user_id)
                duration = f"{status.duration_sec:.2f}s" if status and status.duration_sec is not None else "-"
                tail = "\noutput: truncated" if status and status.output_truncated else ""
                await message.answer(
                    f"Claude code已完成\n"
                    f"task_id: {start.task.task_id}\n"
                    f"exit_code: {event.exit_code}\n"
                    f"duration: {duration}"
                    f"{tail}"
                )
            elif event.type in {EventType.FAILED, EventType.TIMEOUT, EventType.CANCELED}:
                status = await task_service.get_status(start.task.task_id, user_id)
                duration = f"{status.duration_sec:.2f}s" if status and status.duration_sec is not None else "-"
                tail = "\noutput: truncated" if status and status.output_truncated else ""
                error_text = event.error or "-"
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
                    f"任务结束: {event.type.value}\n"
                    f"task_id: {start.task.task_id}\n"
                    f"error: {error_text}\n"
                    f"duration: {duration}"
                    f"{tail}"
                )

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
