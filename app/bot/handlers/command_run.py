from __future__ import annotations

import asyncio
import logging

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
from app.services.diff_generator import DiffGeneratorService
from app.services.result_exporter import ResultExporterService
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
    )
    await presenter.prime(baseline_current_snapshot=True)

    task = asyncio.create_task(streamer.stream_events())
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
        )
