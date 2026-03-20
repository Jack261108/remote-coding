from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventType(str, Enum):
    STARTED = "STARTED"
    STDOUT = "STDOUT"
    STDERR = "STDERR"
    EXITED = "EXITED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELED = "CANCELED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELED = "CANCELED"


FINAL_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.TIMEOUT,
    TaskStatus.CANCELED,
}


@dataclass
class CLIEvent:
    type: EventType
    task_id: str
    content: str | None = None
    exit_code: int | None = None
    error: str | None = None
    at: datetime = field(default_factory=utc_now)


@dataclass
class ExecutionTask:
    task_id: str
    session_id: str
    user_id: int
    provider: str
    prompt: str
    workdir: str
    timeout_sec: int


@dataclass
class TaskRecord:
    task_id: str
    session_id: str
    user_id: int
    provider: str
    prompt: str
    workdir: str
    timeout_sec: int
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    exit_code: int | None = None
    failure_reason: str | None = None
    output_chars: int = 0
    output_truncated: bool = False

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES

    @property
    def duration_sec(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.ended_at or utc_now()
        return max(0.0, (end - self.started_at).total_seconds())


@dataclass
class SessionContext:
    user_id: int
    session_id: str
    provider: str
    workdir: str
    terminal_mode: bool = False
    terminal_id: str | None = None
    claude_chat_active: bool = False
    updated_at: datetime = field(default_factory=utc_now)
