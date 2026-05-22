from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal


class SessionOrigin(str, Enum):
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


@dataclass
class ExternalBinding:
    session_id: str
    user_id: int
    cwd: str
    bound_at: datetime
    jsonl_path: str | None


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
