from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain.models import utc_now

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ALLOWED_HOOK_EVENTS = {
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Notification",
    "Stop",
    "SubagentStop",
    "SessionStart",
    "SessionEnd",
    "PreCompact",
    "PostToolUseFailure",
    "SubagentStart",
    "PostCompact",
    "StopFailure",
    "PermissionDenied",
}
_ALLOWED_HOOK_STATUSES = {
    "starting",
    "processing",
    "running",
    "running_tool",
    "waiting_for_approval",
    "waiting_for_input",
    "ended",
    "failed",
}


def _validate_text(value: object, *, field_name: str, max_length: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须为字符串")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError(f"{field_name} 不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field_name} 过长")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in text):
        raise ValueError(f"{field_name} 包含非法控制字符")
    return text


def validate_path_component(value: object, *, field_name: str, max_length: int = 128) -> str:
    text = _validate_text(value, field_name=field_name, max_length=max_length)
    if text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"{field_name} 包含非法路径字符")
    if _SESSION_ID_RE.fullmatch(text) is None:
        raise ValueError(f"{field_name} 格式非法")
    return text


def validate_session_id(value: object, *, field_name: str = "session_id") -> str:
    return validate_path_component(value, field_name=field_name, max_length=128)


def _validate_hook_cwd(value: object) -> str:
    cwd = _validate_text(value, field_name="cwd", max_length=4096)
    if not Path(cwd).is_absolute():
        raise ValueError("cwd 必须为绝对路径")
    return cwd


def _validate_optional_text(value: object, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _validate_text(value, field_name=field_name, max_length=max_length)


def _validate_optional_path_component(value: object, *, field_name: str, max_length: int = 128) -> str | None:
    if value is None:
        return None
    return validate_path_component(value, field_name=field_name, max_length=max_length)


def _validate_hook_event(value: object) -> str:
    event = _validate_text(value, field_name="event", max_length=64)
    if event not in _ALLOWED_HOOK_EVENTS:
        raise ValueError("event 不受支持")
    return event


def _validate_hook_status(value: object) -> str:
    status = _validate_text(value, field_name="status", max_length=64)
    if status not in _ALLOWED_HOOK_STATUSES:
        raise ValueError("status 不受支持")
    return status


def _validate_optional_pid(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("pid 必须为非负整数")
    if value < 0:
        raise ValueError("pid 必须为非负整数")
    return value


def _validate_tool_input(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("tool_input 必须为对象")
    if not all(isinstance(key, str) for key in value):
        raise ValueError("tool_input 键必须为字符串")
    return value


@dataclass(slots=True)
class HookEvent:
    session_id: str
    cwd: str
    event: str
    status: str
    pid: int | None = None
    tty: str | None = None
    tool: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    notification_type: str | None = None
    message: str | None = None

    @property
    def expects_response(self) -> bool:
        return self.event == "PermissionRequest" and self.status == "waiting_for_approval"

    def with_tool_use_id(self, tool_use_id: str) -> HookEvent:
        return replace(self, tool_use_id=tool_use_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HookEvent:
        if not isinstance(data, dict):
            raise ValueError("hook payload 必须为对象")
        return cls(
            session_id=validate_session_id(data.get("session_id")),
            cwd=_validate_hook_cwd(data.get("cwd")),
            event=_validate_hook_event(data.get("event")),
            status=_validate_hook_status(data.get("status")),
            pid=_validate_optional_pid(data.get("pid")),
            tty=_validate_optional_text(data.get("tty"), field_name="tty", max_length=512),
            tool=_validate_optional_text(data.get("tool"), field_name="tool", max_length=128),
            tool_input=_validate_tool_input(data.get("tool_input")),
            tool_use_id=_validate_optional_path_component(data.get("tool_use_id"), field_name="tool_use_id"),
            notification_type=_validate_optional_text(data.get("notification_type"), field_name="notification_type", max_length=128),
            message=_validate_optional_text(data.get("message"), field_name="message", max_length=8192),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "event": self.event,
            "status": self.status,
            "pid": self.pid,
            "tty": self.tty,
            "tool": self.tool,
            "tool_input": self.tool_input,
            "tool_use_id": self.tool_use_id,
            "notification_type": self.notification_type,
            "message": self.message,
        }


@dataclass(slots=True)
class HookResponse:
    decision: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        decision_payload: dict[str, Any] = {"behavior": self.decision}
        if self.decision == "deny" and self.reason:
            decision_payload["message"] = self.reason
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision_payload,
            }
        }


@dataclass(slots=True)
class PendingPermissionRequest:
    session_id: str
    tool_use_id: str
    writer: Any
    event: HookEvent
    received_at: datetime = field(default_factory=utc_now)
