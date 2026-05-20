from __future__ import annotations

import logging
from collections import defaultdict

from aiogram import F, Router
from aiogram.types import Message

from app.domain.file_models import FileUploadResult, FileValidationError
from app.domain.models import TaskStatus
from app.services.file_receiver import FileReceiverService
from app.services.session_service import SessionService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

# In-memory queue for uploads received while a task is running.
# Maps user_id -> list of (filename, data) tuples waiting to be processed.
_pending_uploads: dict[int, list[tuple[str, bytes]]] = defaultdict(list)


def _format_size(size_bytes: int) -> str:
    """Format byte size into human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


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
    user_id: int,
) -> None:
    """Process any queued uploads for a user after their task completes."""
    pending = _pending_uploads.pop(user_id, [])
    for filename, data in pending:
        await _process_upload(
            message,
            file_receiver=file_receiver,
            session_service=session_service,
            filename=filename,
            data=data,
        )


def register_file_upload_handler(
    router: Router,
    *,
    file_receiver: FileReceiverService,
    session_service: SessionService,
    task_service: TaskService,
) -> None:
    @router.message(F.document)
    async def handle_document(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        document = message.document
        if document is None:
            return

        filename = document.file_name or "unnamed_file"

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

        # Queue if task is running
        if await _user_has_running_task(task_service, user_id):
            _pending_uploads[user_id].append((filename, data))
            await message.answer(f"⏳ 任务运行中，文件 {filename} 已加入队列，将在任务完成后处理。")
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

        # Queue if task is running
        if await _user_has_running_task(task_service, user_id):
            _pending_uploads[user_id].append((filename, data))
            await message.answer(f"⏳ 任务运行中，文件 {filename} 已加入队列，将在任务完成后处理。")
            return

        await _process_upload(
            message,
            file_receiver=file_receiver,
            session_service=session_service,
            filename=filename,
            data=data,
        )
