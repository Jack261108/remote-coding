from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from app.domain.models import utc_now


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

    def with_tool_use_id(self, tool_use_id: str) -> "HookEvent":
        return replace(self, tool_use_id=tool_use_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HookEvent":
        tool_input = data.get("tool_input")
        if tool_input is not None and not isinstance(tool_input, dict):
            raise ValueError("tool_input 必须为对象")
        return cls(
            session_id=str(data["session_id"]),
            cwd=str(data["cwd"]),
            event=str(data["event"]),
            status=str(data["status"]),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            tty=str(data["tty"]) if data.get("tty") is not None else None,
            tool=str(data["tool"]) if data.get("tool") is not None else None,
            tool_input=tool_input,
            tool_use_id=str(data["tool_use_id"]) if data.get("tool_use_id") is not None else None,
            notification_type=str(data["notification_type"]) if data.get("notification_type") is not None else None,
            message=str(data["message"]) if data.get("message") is not None else None,
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
class PendingPermission:
    session_id: str
    tool_use_id: str
    writer: Any
    event: HookEvent
    received_at: datetime = field(default_factory=utc_now)
