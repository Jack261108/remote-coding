from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiogram.types import BufferedInputFile, FSInputFile, Message

from app.bot.handlers.run_presenter_dispatcher import PresenterOutputDispatcher
from app.bot.handlers.run_telegram_messenger import RunTelegramMessenger
from app.bot.presenters.structured_reply_presenter import StructuredReplyPresenter, normalize_stream_text
from app.domain.models import EventType
from app.services.diff_generator import DiffGeneratorService
from app.services.result_exporter import ResultExporterService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


def _load_gitignore_patterns(workdir: str) -> list[str]:
    """Load gitignore patterns from workdir/.gitignore."""
    gitignore_path = Path(workdir) / ".gitignore"
    if not gitignore_path.is_file():
        return []
    patterns: list[str] = []
    try:
        for line in gitignore_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    except OSError:
        logger.warning("Failed to read .gitignore at %s", gitignore_path)
    return patterns


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
_INTERACTIVE_PUMP_CANCEL_GRACE_SEC = 5.0
_ABANDONED_INTERACTIVE_PUMP_TASKS: set[asyncio.Task] = set()


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
        diff_generator: DiffGeneratorService | None = None,
        result_exporter: ResultExporterService | None = None,
        queued_upload_scheduler: Callable[[], None] | None = None,
    ) -> None:
        self._start = start
        self._task_service = task_service
        self._user_id = user_id
        self._presenter = presenter
        self._dispatcher = dispatcher
        self._messenger = messenger
        self._lifecycle_message = lifecycle_message
        self._diff_generator = diff_generator
        self._result_exporter = result_exporter
        self._queued_upload_scheduler = queued_upload_scheduler
        self._queued_upload_scheduled = False
        self._interactive_pump: asyncio.Task | None = None
        self._spinner_task: asyncio.Task | None = None
        self._emit_lock = asyncio.Lock()
        self._pre_snapshot: dict[Path, float] | None = None
        self._gitignore_patterns: list[str] = []

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

    def _schedule_queued_uploads_once(self) -> None:
        if self._queued_upload_scheduler is None or self._queued_upload_scheduled:
            return
        self._queued_upload_scheduled = True
        try:
            self._queued_upload_scheduler()
        except Exception:
            logger.exception("failed to schedule queued upload processing", extra={"user_id": self._user_id})

    @staticmethod
    def _consume_task_result(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass

    def _forget_abandoned_interactive_pump(self, task: asyncio.Task) -> None:
        _ABANDONED_INTERACTIVE_PUMP_TASKS.discard(task)
        if self._interactive_pump is task:
            self._interactive_pump = None
        self._consume_task_result(task)

    async def _cancel_interactive_pump(self, *, timeout_sec: float | None = None) -> None:
        task = self._interactive_pump
        if task is None:
            return
        task.cancel()
        if timeout_sec is None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            if task.done() and self._interactive_pump is task:
                self._interactive_pump = None
            return

        done, _ = await asyncio.wait({task}, timeout=timeout_sec)
        if task in done:
            try:
                await task
            except asyncio.CancelledError:
                pass
            if self._interactive_pump is task:
                self._interactive_pump = None
            return
        if task not in _ABANDONED_INTERACTIVE_PUMP_TASKS:
            _ABANDONED_INTERACTIVE_PUMP_TASKS.add(task)
            task.add_done_callback(self._forget_abandoned_interactive_pump)
        logger.error(
            "interactive pump cancellation grace timeout",
            extra={"task_id": self._start.task.task_id, "user_id": self._user_id, "timeout_sec": timeout_sec},
        )

    async def force_cleanup(self, *, schedule_uploads: bool = False, cancel_timeout_sec: float | None = None) -> None:
        if schedule_uploads:
            self._schedule_queued_uploads_once()
        await self._stop_spinner()
        await self._cancel_interactive_pump(timeout_sec=cancel_timeout_sec)

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

    def _capture_diff_snapshot(self) -> None:
        """Capture pre-task filesystem snapshot for diff generation (non-blocking)."""
        if self._diff_generator is None:
            return
        try:
            workdir = self._start.task.workdir
            self._gitignore_patterns = _load_gitignore_patterns(workdir)
            self._pre_snapshot = self._diff_generator.capture_snapshot(workdir, self._gitignore_patterns)
        except Exception:
            logger.exception("diff snapshot capture failed, skipping diff generation")
            self._pre_snapshot = None

    async def _generate_and_send_diff(self) -> None:
        """Generate diff after successful task and send via Telegram (non-blocking)."""
        if self._diff_generator is None or self._pre_snapshot is None:
            return
        try:
            workdir = self._start.task.workdir
            modified_files = self._diff_generator.detect_modified_files(
                workdir=workdir,
                pre_snapshot=self._pre_snapshot,
                gitignore_patterns=self._gitignore_patterns,
            )
            diff_result = self._diff_generator.generate_unified_diff(modified_files, self._pre_snapshot)
            if diff_result is None:
                return

            if diff_result.is_patch_file:
                # Send as .patch file attachment
                patch_bytes = diff_result.content.encode("utf-8")
                short_id = self._start.task.task_id[:8]
                filename = f"{short_id}.patch"
                doc = BufferedInputFile(file=patch_bytes, filename=filename)
                await self._messenger._root_message.answer_document(doc, caption=f"📎 Diff ({diff_result.file_count} files)")
            else:
                # Send as code-block formatted message
                diff_msg = f"```diff\n{diff_result.content}\n```"
                await self._messenger.send_message_safely(diff_msg)
        except Exception:
            logger.exception("diff generation/send failed, skipping")

    async def _maybe_auto_export(self) -> None:
        """Check output size and auto-export as Markdown document if threshold exceeded."""
        if self._result_exporter is None:
            return
        try:
            record = await self._task_service.get_status(self._start.task.task_id, self._user_id)
            if record is None:
                return
            if not self._result_exporter.should_auto_export(record.output_chars):
                return
            export_result = await self._result_exporter.export_markdown(record)
            try:
                doc = FSInputFile(path=export_result.file_path, filename=export_result.filename)
                await self._messenger._root_message.answer_document(doc)
            finally:
                # Clean up temp file
                export_result.file_path.unlink(missing_ok=True)
                try:
                    export_result.file_path.parent.rmdir()
                except OSError:
                    logger.debug(
                        "temp export directory cleanup failed (non-fatal)",
                        extra={"dir": str(export_result.file_path.parent)},
                        exc_info=True,
                    )
        except Exception:
            logger.warning(
                "auto-export failed",
                extra={"task_id": self._start.task.task_id, "user_id": self._user_id},
                exc_info=True,
            )

    async def stream_events(self) -> None:
        saw_exit = False
        saw_terminal = False
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
                    self._capture_diff_snapshot()
                    self._start_spinner()
                    if self._start.interactive and self._interactive_pump is None:
                        self._interactive_pump = asyncio.create_task(self.pump_structured_reply())
                    continue

                if event.type in {EventType.EXITED, EventType.FAILED, EventType.TIMEOUT, EventType.CANCELED}:
                    saw_terminal = True
                if event.type == EventType.EXITED:
                    saw_exit = True

                if self._start.interactive:
                    async with self._emit_lock:
                        await self._dispatcher.emit_presenter_messages(log_missing=True)
                await self._dispatcher.flush()
                await self._stop_spinner()
                duration, truncated = await _load_status_summary(self._task_service, self._start.task.task_id, self._user_id)

                if event.type == EventType.EXITED:
                    success_msg = _build_success_message(
                        task_id=self._start.task.task_id,
                        exit_code=event.exit_code,
                        duration=duration,
                        truncated=truncated,
                    )
                    if not await self._messenger.edit_message_safely(self._lifecycle_message, success_msg):
                        await self._messenger.answer_safely(success_msg)
                    await self._maybe_auto_export()
                    # Generate and send diff on successful completion
                    await self._generate_and_send_diff()
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
            if saw_terminal:
                self._schedule_queued_uploads_once()
            try:
                await self._stop_spinner()
                if saw_exit and self._start.interactive:
                    await asyncio.sleep(0.1)
                    # Freeze the presenter's last turn ID to prevent emitting
                    # new turns that arrive after task completion (e.g., idle greetings).
                    self._presenter.freeze_reply_cursor()
                    async with self._emit_lock:
                        await self._dispatcher.emit_presenter_messages(final=True, log_missing=True)
                    await self._dispatcher.flush()
            finally:
                await self._cancel_interactive_pump(timeout_sec=_INTERACTIVE_PUMP_CANCEL_GRACE_SEC)
