from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal


class SessionOrigin(StrEnum):
    TMUX = "tmux"
    EXTERNAL = "external"


@dataclass
class OwnershipResult:
    owner_user_id: int | None
    origin: SessionOrigin
    ownership_state: Literal["owned", "bound", "unbound"]


@dataclass
class UnboundExternalSession:
    session_id: str
    cwd: str
    pid: int | None
    first_seen: datetime
    last_seen: datetime
    event_count: int
    title: str | None = None


@dataclass
class ExternalBinding:
    session_id: str
    user_id: int
    cwd: str
    bound_at: datetime
    jsonl_path: str | None
    pid: int | None = None
    last_activity_at_init: InitVar[datetime | None] = None
    last_activity_at: datetime = field(init=False)

    def __post_init__(self, last_activity_at_init: datetime | None) -> None:
        # Default activity timestamp to bind time so existing callers that don't
        # pass `last_activity_at` get a sensible non-None value. The stored
        # attribute is always a `datetime`, never None.
        self.last_activity_at = last_activity_at_init if last_activity_at_init is not None else self.bound_at


@dataclass
class UnboundPermissionState:
    session_id: str
    tool_use_id: str
    notified_user_ids: list[int]
    responded: bool
    responded_by: int | None
    created_at: datetime


@dataclass
class BindResult:
    success: bool
    message: str
    session_id: str | None = None
    jsonl_path: Path | None = None
    conversation_available: bool = False
