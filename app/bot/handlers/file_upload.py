from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.types import Message

from app.domain.file_models import FileUploadResult, FileValidationError
from app.domain.models import TaskStatus
from app.services.file_receiver import FileReceiverService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from app.services.upload_queue import UploadQueueManager

logger = logging.getLogger(__name__)
_ACTIVE_UPLOAD_TASKS: set[asyncio.Task[None]] = set()


def _format_size(size_bytes: int) -> str:
    """Format byte size into human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def _max_upload_size_bytes(upload_max_file_size_mb: int) -> int:
    return upload_max_file_size_mb * 1024 * 1024


def _metadata_exceeds_limit(file_size: int | None, *, max_size_bytes: int) -> bool:
    return file_size is not None and file_size > max_size_bytes


async def _answer_oversized(message: Message, *, filename: str, size_bytes: int, upload_max_file_size_mb: int) -> None:
    await message.answer(f"❌ 文件被拒绝: {filename}\n原因: 文件大小 {_format_size(size_bytes)} 超过 {upload_max_file_size_mb} MB 限制。")


async def _user_has_running_task(task_service: TaskService, user_id: int) -> bool:
    """Check if the user has a task currently in RUNNING or PENDING state."""
    recent = await task_service.list_recent(user_id, limit=5)
    return any(t.status in (TaskStatus.RUNNING, TaskStatus.PENDING) for t in recent)


async def _process_upload(
    message: Message,
    *,
    file_receiver: FileReceiverService,
    session_service: SessionService,
    filename: str,
    data: bytes,
) -> None:
    """Validate and store a file, then reply with result."""
    user_id = message.from_user.id if message.from_user else 0
    session = await session_service.get(user_id)
    if session is None:
        await message.answer("请先使用 /session 或 /claude 创建会话后再上传文件。")
        return

    workdir = session.workdir

    result = await file_receiver.receive_file(
        user_id=user_id,
        workdir=workdir,
        filename=filename,
        data=data,
    )

    if isinstance(result, FileUploadResult):
        size_str = _format_size(result.size_bytes)
        await message.answer(f"✅ 文件已接收: {result.filename} ({size_str})")
    elif isinstance(result, FileValidationError):
        await message.answer(f"❌ 文件被拒绝: {result.filename}\n原因: {result.reason}")


async def process_pending_uploads(
    message: Message,
    *,
    file_receiver: FileReceiverService,
    session_service: SessionService,
    upload_queue: UploadQueueManager,
    user_id: int,
) -> None:
    """Process any queued uploads for a user after their task completes."""
    pending = await upload_queue.drain(user_id=user_id)
    for item in pending:
        try:
            await _process_upload(
                message,
                file_receiver=file_receiver,
                session_service=session_service,
                filename=item.filename,
                data=item.data,
            )
        except Exception:
            logger.exception("queued upload processing failed", extra={"user_id": user_id, "filename": item.filename})


def schedule_pending_upload_processing(
    message: Message,
    *,
    file_receiver: FileReceiverService,
    session_service: SessionService,
    upload_queue: UploadQueueManager,
    user_id: int,
) -> asyncio.Task[None]:
    """Schedule queued uploads to be processed in the background."""
    task: asyncio.Task[None] = asyncio.create_task(
        process_pending_uploads(
            message,
            file_receiver=file_receiver,
            session_service=session_service,
            upload_queue=upload_queue,
            user_id=user_id,
        )
    )
    _ACTIVE_UPLOAD_TASKS.add(task)

    def _on_done(done_task: asyncio.Task[None]) -> None:
        _ACTIVE_UPLOAD_TASKS.discard(done_task)
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is None:
            return
        logger.error(
            "queued upload background task failed",
            extra={"user_id": user_id, "error": str(exc)},
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    task.add_done_callback(_on_done)
    return task


def register_file_upload_handler(
    router: Router,
    *,
    file_receiver: FileReceiverService,
    session_service: SessionService,
    task_service: TaskService,
    upload_queue: UploadQueueManager,
    upload_max_file_size_mb: int,
) -> None:
    @router.message(F.document)
    async def handle_document(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        document = message.document
        if document is None:
            return

        filename = document.file_name or "unnamed_file"
        max_size_bytes = _max_upload_size_bytes(upload_max_file_size_mb)
        file_size = document.file_size
        if _metadata_exceeds_limit(file_size, max_size_bytes=max_size_bytes):
            await _answer_oversized(
                message,
                filename=filename,
                size_bytes=file_size,
                upload_max_file_size_mb=upload_max_file_size_mb,
            )
            return

        # Download file via Telegram Bot API
        try:
            bot = message.bot
            if bot is None:
                await message.answer("❌ 内部错误: 无法获取 bot 实例。")
                return
            file_obj = await bot.get_file(document.file_id)
            if file_obj.file_path is None:
                await message.answer("❌ 下载失败: Telegram 未返回文件路径。")
                return
            bio = await bot.download_file(file_obj.file_path)
            if bio is None:
                await message.answer("❌ 下载失败: 无法从 Telegram 下载文件。")
                return
            data = bio.read()
        except Exception as exc:
            logger.exception("Failed to download document from Telegram", extra={"user_id": user_id, "filename": filename})
            await message.answer(f"❌ 文件下载失败: {exc}")
            return

        if len(data) > max_size_bytes:
            await _answer_oversized(
                message,
                filename=filename,
                size_bytes=len(data),
                upload_max_file_size_mb=upload_max_file_size_mb,
            )
            return

        # Queue if task is running
        if await _user_has_running_task(task_service, user_id):
            queued = await upload_queue.enqueue(user_id=user_id, filename=filename, data=data)
            if not queued.accepted:
                await message.answer(f"❌ 文件未加入队列: {filename}\n原因: {queued.reason}")
                return
            await message.answer(
                f"⏳ 任务运行中，文件 {filename} 已加入队列，将在任务完成后处理。\n"
                "注意：队列仅保存在内存中，如果 bot 在任务完成前重启，已排队文件会丢失。"
            )
            return

        await _process_upload(
            message,
            file_receiver=file_receiver,
            session_service=session_service,
            filename=filename,
            data=data,
        )

    @router.message(F.photo)
    async def handle_photo(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not message.photo:
            return

        # Use the largest resolution photo (last in array)
        photo = message.photo[-1]
        filename = f"photo_{photo.file_unique_id}.jpg"
        max_size_bytes = _max_upload_size_bytes(upload_max_file_size_mb)
        file_size = photo.file_size
        if _metadata_exceeds_limit(file_size, max_size_bytes=max_size_bytes):
            await _answer_oversized(
                message,
                filename=filename,
                size_bytes=file_size,
                upload_max_file_size_mb=upload_max_file_size_mb,
            )
            return

        try:
            bot = message.bot
            if bot is None:
                await message.answer("❌ 内部错误: 无法获取 bot 实例。")
                return
            file_obj = await bot.get_file(photo.file_id)
            if file_obj.file_path is None:
                await message.answer("❌ 下载失败: Telegram 未返回文件路径。")
                return
            bio = await bot.download_file(file_obj.file_path)
            if bio is None:
                await message.answer("❌ 下载失败: 无法从 Telegram 下载文件。")
                return
            data = bio.read()
        except Exception as exc:
            logger.exception("Failed to download photo from Telegram", extra={"user_id": user_id})
            await message.answer(f"❌ 文件下载失败: {exc}")
            return

        if len(data) > max_size_bytes:
            await _answer_oversized(
                message,
                filename=filename,
                size_bytes=len(data),
                upload_max_file_size_mb=upload_max_file_size_mb,
            )
            return

        # Queue if task is running
        if await _user_has_running_task(task_service, user_id):
            queued = await upload_queue.enqueue(user_id=user_id, filename=filename, data=data)
            if not queued.accepted:
                await message.answer(f"❌ 文件未加入队列: {filename}\n原因: {queued.reason}")
                return
            await message.answer(
                f"⏳ 任务运行中，文件 {filename} 已加入队列，将在任务完成后处理。\n"
                "注意：队列仅保存在内存中，如果 bot 在任务完成前重启，已排队文件会丢失。"
            )
            return

        await _process_upload(
            message,
            file_receiver=file_receiver,
            session_service=session_service,
            filename=filename,
            data=data,
        )
