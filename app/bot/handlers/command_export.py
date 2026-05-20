"""Handler for /export <task_id> [--zip] command."""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, Message

from app.services.result_exporter import ResultExporterService, ZipSizeLimitError
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


def parse_export_args(args: str | None) -> tuple[str | None, bool]:
    """Parse export command arguments.

    Returns (task_id, use_zip) tuple.
    """
    if not args or not args.strip():
        return None, False

    parts = args.strip().split()
    task_id = parts[0]
    use_zip = "--zip" in parts[1:]
    return task_id, use_zip


def register_export_handler(
    router,
    *,
    task_service: TaskService,
    result_exporter: ResultExporterService,
):
    @router.message(Command("export"))
    async def command_export(message: Message, command: CommandObject) -> None:
        user_id = message.from_user.id if message.from_user else 0
        task_id, use_zip = parse_export_args(command.args)

        if not task_id:
            await message.answer("用法: /export <task_id> [--zip]")
            return

        # Look up task and verify ownership
        record = await task_service.get_status(task_id, user_id)
        if record is None:
            await message.answer(f"任务不存在或无权访问: {task_id}")
            return

        try:
            if use_zip:
                if record.started_at is None or record.ended_at is None:
                    await message.answer("任务尚未完成，无法生成 ZIP 导出")
                    return
                export_result = await result_exporter.export_zip(
                    record,
                    workdir=record.workdir,
                    started_at=record.started_at,
                    ended_at=record.ended_at,
                )
            else:
                export_result = await result_exporter.export_markdown(record)

            # Send as Telegram document
            doc = FSInputFile(path=export_result.file_path, filename=export_result.filename)
            await message.answer_document(doc)

        except ZipSizeLimitError as exc:
            await message.answer(f"导出失败: {exc}")
        except Exception:
            logger.exception("export failed", extra={"task_id": task_id, "user_id": user_id})
            await message.answer("导出时发生错误，请稍后重试")
        finally:
            # Clean up temp files
            try:
                if "export_result" in locals() and export_result.file_path.exists():
                    parent = export_result.file_path.parent
                    export_result.file_path.unlink(missing_ok=True)
                    # Remove temp dir if empty
                    if parent != Path("/") and not any(parent.iterdir()):
                        parent.rmdir()
            except Exception:
                logger.debug("cleanup of temp export file failed", exc_info=True)
