"""State tracker for pending AskUserQuestion prompts in external sessions.

Holds references so that when a Telegram user clicks an option button,
we can look up the session PID, tmux pane, and permission details needed
to inject the answer and respond to the hook.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.domain.models import utc_now
from app.domain.user_question_models import UserQuestionPrompt

logger = logging.getLogger(__name__)

_TTL_SEC = 300.0  # 5 minutes


@dataclass(slots=True)
class PendingExternalUserQuestion:
    tool_use_id: str
    session_id: str
    user_id: int
    pid: int | None
    prompts: tuple[UserQuestionPrompt, ...]
    pane_id: str | None
    tmux_bin: str = "tmux"
    created_at: datetime = field(default_factory=utc_now)


class ExternalUserQuestionState:
    """In-memory store for pending external AskUserQuestion interactions."""

    def __init__(self, *, ttl_sec: float = _TTL_SEC) -> None:
        self._ttl_sec = ttl_sec
        # Keyed by tool_use_id
        self._pending: dict[str, PendingExternalUserQuestion] = {}

    def store(self, pending: PendingExternalUserQuestion) -> None:
        self._prune_stale()
        self._pending[pending.tool_use_id] = pending
        logger.debug(
            "stored pending external user question",
            extra={"tool_use_id": pending.tool_use_id, "session_id": pending.session_id},
        )

    def get(self, tool_use_id: str) -> PendingExternalUserQuestion | None:
        self._prune_stale()
        return self._pending.get(tool_use_id)

    def remove(self, tool_use_id: str) -> PendingExternalUserQuestion | None:
        return self._pending.pop(tool_use_id, None)

    def _prune_stale(self) -> None:
        now = utc_now()
        stale_keys = [key for key, pending in self._pending.items() if (now - pending.created_at).total_seconds() > self._ttl_sec]
        for key in stale_keys:
            self._pending.pop(key, None)
