from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.domain.models import utc_now


class TranscriptEntryKind(str, Enum):
    RAW = "raw"
    NORMALIZED = "normalized"
    NOISE_DROPPED = "noise_dropped"
    PHASE = "phase"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"


@dataclass
class TranscriptEntry:
    seq: int
    kind: TranscriptEntryKind
    text: str = ""
    turn_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "text": self.text,
            "turn_id": self.turn_id,
            "payload": self.payload,
            "at": self.at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptEntry":
        return cls(
            seq=int(data["seq"]),
            kind=TranscriptEntryKind(data["kind"]),
            text=str(data.get("text", "")),
            turn_id=data.get("turn_id"),
            payload=dict(data.get("payload", {})),
            at=datetime.fromisoformat(data["at"]) if data.get("at") else utc_now(),
        )
