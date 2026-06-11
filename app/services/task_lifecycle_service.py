from __future__ import annotations

import logging

from app.domain.models import CLIEvent, EventType, TaskRecord, TaskStatus, utc_now


def apply_task_event(
    *,
    record: TaskRecord,
    event: CLIEvent,
    output_char_limit: int,
    logger: logging.Logger,
    log_extra: dict[str, object] | None = None,
) -> None:
    if event.type == EventType.STARTED:
        record.status = TaskStatus.RUNNING
        record.started_at = record.started_at or utc_now()
        return

    if event.type in {EventType.STDOUT, EventType.STDERR}:
        content = event.content or ""
        if event.type == EventType.STDERR and content:
            content = f"[stderr] {content}"

        if record.output_chars >= output_char_limit:
            event.content = ""
            record.output_truncated = True
            return

        remaining = output_char_limit - record.output_chars
        if len(content) > remaining:
            content = content[:remaining]
            event.content = content
            record.output_chars += remaining
            record.output_truncated = True
        else:
            record.output_chars += len(content)

        record.output_text += content
        return

    record.ended_at = utc_now()

    if event.type == EventType.EXITED:
        record.status = TaskStatus.SUCCEEDED
        record.exit_code = event.exit_code
        record.failure_reason = None
    elif event.type == EventType.CANCELED:
        record.status = TaskStatus.CANCELED
        record.failure_reason = event.error
    elif event.type == EventType.TIMEOUT:
        record.status = TaskStatus.TIMEOUT
        record.failure_reason = event.error
    elif event.type == EventType.FAILED:
        record.status = TaskStatus.FAILED
        record.exit_code = event.exit_code
        record.failure_reason = event.error

    payload = {
        "task_id": record.task_id,
        "user_id": record.user_id,
        "provider": record.provider,
        "status": record.status.value,
        "duration_sec": record.duration_sec,
        "exit_code": record.exit_code,
        "failure_reason": record.failure_reason,
    }
    if log_extra:
        payload.update(log_extra)

    if record.status == TaskStatus.SUCCEEDED:
        logger.info("task completed", extra=payload)
    else:
        logger.error("task completed with error", extra=payload)
