from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


def utc_now() -> datetime:
    return datetime.now(UTC)


class EventType(StrEnum):
    STARTED = "STARTED"
    STDOUT = "STDOUT"
    STDERR = "STDERR"
    EXITED = "EXITED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELED = "CANCELED"


class TaskStatus(StrEnum):
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
    claude_session_id: str | None = None
    extra_cli_args: list[str] = field(default_factory=list)


@dataclass
class TaskRecord:
    task_id: str
    session_id: str
    user_id: int
    provider: str
    prompt: str
    workdir: str
    timeout_sec: int
    claude_session_id: str | None = None
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
    claude_session_id: str | None = None
    updated_at: datetime = field(default_factory=utc_now)
    attached_user_ids: list[int] = field(default_factory=list)
    is_owner: bool = True

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "provider": self.provider,
            "workdir": self.workdir,
            "terminal_mode": self.terminal_mode,
            "terminal_id": self.terminal_id,
            "claude_chat_active": self.claude_chat_active,
            "claude_session_id": self.claude_session_id,
            "updated_at": self.updated_at.isoformat(),
            "attached_user_ids": list(self.attached_user_ids),
            "is_owner": self.is_owner,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> SessionContext:
        return cls(
            user_id=int(payload["user_id"]),
            session_id=str(payload["session_id"]),
            provider=str(payload["provider"]),
            workdir=str(payload["workdir"]),
            terminal_mode=bool(payload.get("terminal_mode", False)),
            terminal_id=str(payload["terminal_id"]) if payload.get("terminal_id") is not None else None,
            claude_chat_active=bool(payload.get("claude_chat_active", False)),
            claude_session_id=str(payload["claude_session_id"]) if payload.get("claude_session_id") is not None else None,
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            attached_user_ids=[int(uid) for uid in payload.get("attached_user_ids", [])],
            is_owner=bool(payload.get("is_owner", True)),
        )


@dataclass
class TerminalSessionInfo:
    """View-model for session listing (/list command)."""

    terminal_id: str
    tmux_session_name: str
    workdir: str
    phase: str
    owner_user_id: int | None
    attached_user_ids: list[int]
    is_alive: bool
