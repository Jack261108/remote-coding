from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AutoApproveActivation:
    session_id: str
    user_id: int
    activated_at: datetime


@dataclass(frozen=True, slots=True)
class ActivationSlot:
    session_id: str
    holder_user_id: int
    attempt_id: str


@dataclass(frozen=True, slots=True)
class SlotClaimed:
    attempt_id: str


@dataclass(frozen=True, slots=True)
class SlotConflict:
    holder_user_id: int


@dataclass(frozen=True, slots=True)
class SlotAlreadyClaimedBySameUser:
    attempt_id: str


@dataclass(frozen=True, slots=True)
class SlotActiveOwnerExists:
    owner_user_id: int


SlotClaimResult = SlotClaimed | SlotConflict | SlotAlreadyClaimedBySameUser | SlotActiveOwnerExists


@dataclass(frozen=True, slots=True)
class CommitSlotSucceeded:
    pass


@dataclass(frozen=True, slots=True)
class CommitSlotSessionEnded:
    pass


@dataclass(frozen=True, slots=True)
class CommitSlotMismatch:
    pass


CommitSlotResult = CommitSlotSucceeded | CommitSlotSessionEnded | CommitSlotMismatch


@dataclass(slots=True)
class AutoApproveEntry:
    session_id: str
    user_id: int
    activated_at: datetime


class AutoApproveService:
    """Manages per-session auto-approve state (in-memory only)."""

    def __init__(self) -> None:
        self._sessions: dict[str, AutoApproveEntry] = {}

    def is_active(self, session_id: str) -> bool:
        """Check if auto-approve is active for the given session."""
        return session_id in self._sessions

    def activate(self, session_id: str, *, user_id: int) -> None:
        """Enable auto-approve for the given session."""
        self._sessions[session_id] = AutoApproveEntry(
            session_id=session_id,
            user_id=user_id,
            activated_at=datetime.now(timezone.utc),
        )
        logger.info("Auto-approve activated for session %s by user %d", session_id, user_id)

    def deactivate(self, session_id: str) -> bool:
        """Disable auto-approve. Returns True if it was active."""
        entry = self._sessions.pop(session_id, None)
        if entry is not None:
            logger.info("Auto-approve deactivated for session %s", session_id)
            return True
        return False

    def clear_session(self, session_id: str) -> None:
        """Clear state for a session (called on SessionEnd/cleanup)."""
        self._sessions.pop(session_id, None)

    def get_active_session_for_user(self, user_id: int, session_id: str) -> bool:
        """Check if the given session_id has auto-approve active for the user."""
        entry = self._sessions.get(session_id)
        if entry is None:
            return False
        return entry.user_id == user_id
