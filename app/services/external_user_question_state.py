"""In-memory state for pending AskUserQuestion prompts in external sessions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.domain.models import utc_now
from app.domain.user_question_models import (
    ExternalGhosttyQuestionTarget,
    ExternalUserQuestionPhase,
    ExternalUserQuestionTarget,
    UserQuestionPrompt,
)

logger = logging.getLogger(__name__)

_TTL_SEC = 300.0
_NON_ACTIVE_GRACE_MULTIPLIER = 5


@dataclass(slots=True)
class PendingExternalUserQuestion:
    tool_use_id: str
    session_id: str
    user_id: int
    prompts: tuple[UserQuestionPrompt, ...]
    target: ExternalUserQuestionTarget
    phase: ExternalUserQuestionPhase = ExternalUserQuestionPhase.ACTIVE
    failure_reason: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class PendingExternalUserQuestionSnapshot:
    tool_use_id: str
    session_id: str
    user_id: int
    prompts: tuple[UserQuestionPrompt, ...]
    target: ExternalUserQuestionTarget
    phase: ExternalUserQuestionPhase
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: PendingExternalUserQuestion) -> PendingExternalUserQuestionSnapshot:
        return cls(
            tool_use_id=record.tool_use_id,
            session_id=record.session_id,
            user_id=record.user_id,
            prompts=record.prompts,
            target=record.target,
            phase=record.phase,
            failure_reason=record.failure_reason,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


@dataclass(frozen=True, slots=True)
class ExternalQuestionPendingNone:
    pass


@dataclass(frozen=True, slots=True)
class ExternalQuestionPendingUnique:
    pending: PendingExternalUserQuestionSnapshot


@dataclass(frozen=True, slots=True)
class ExternalQuestionPendingAmbiguous:
    count: int


ExternalQuestionPendingResolution = ExternalQuestionPendingNone | ExternalQuestionPendingUnique | ExternalQuestionPendingAmbiguous


class ExternalUserQuestionState:
    """TTL-bounded external question store with generation-safe transitions."""

    def __init__(
        self,
        *,
        ttl_sec: float = _TTL_SEC,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")
        self._ttl_sec = ttl_sec
        self._wall_clock = wall_clock or utc_now
        self._pending: dict[str, PendingExternalUserQuestion] = {}

    def store(self, pending: PendingExternalUserQuestion) -> None:
        self._prune_stale()
        now = self._wall_clock()
        pending.created_at = now
        pending.updated_at = now
        self._pending[pending.tool_use_id] = pending
        logger.debug(
            "stored pending external user question",
            extra={
                "tool_use_id": pending.tool_use_id,
                "session_id": pending.session_id,
                "target_kind": pending.target.kind,
            },
        )

    def get(self, tool_use_id: str) -> PendingExternalUserQuestionSnapshot | None:
        self._prune_stale()
        record = self._pending.get(tool_use_id)
        return PendingExternalUserQuestionSnapshot.from_record(record) if record is not None else None

    def get_active(self, tool_use_id: str) -> PendingExternalUserQuestionSnapshot | None:
        snapshot = self.get(tool_use_id)
        if snapshot is None or snapshot.phase is not ExternalUserQuestionPhase.ACTIVE:
            return None
        return snapshot

    def resolve_unique_active_for_user(
        self,
        user_id: int,
        *,
        kind: Literal["tmux", "ghostty"] = "ghostty",
    ) -> ExternalQuestionPendingResolution:
        self._prune_stale()
        matches = [
            PendingExternalUserQuestionSnapshot.from_record(record)
            for record in self._pending.values()
            if record.user_id == user_id and record.phase is ExternalUserQuestionPhase.ACTIVE and record.target.kind == kind
        ]
        if not matches:
            return ExternalQuestionPendingNone()
        if len(matches) > 1:
            return ExternalQuestionPendingAmbiguous(count=len(matches))
        return ExternalQuestionPendingUnique(pending=matches[0])

    def mark_terminal_action_applied(
        self,
        *,
        tool_use_id: str,
        expected_target: ExternalUserQuestionTarget,
    ) -> bool:
        record = self._pending.get(tool_use_id)
        if record is None or record.phase is not ExternalUserQuestionPhase.ACTIVE or record.target != expected_target:
            return False
        record.phase = ExternalUserQuestionPhase.TERMINAL_ACTION_APPLIED
        record.updated_at = self._wall_clock()
        return True

    def mark_completed(
        self,
        *,
        tool_use_id: str,
        expected_target: ExternalUserQuestionTarget,
    ) -> bool:
        record = self._pending.get(tool_use_id)
        if record is None or record.phase is not ExternalUserQuestionPhase.TERMINAL_ACTION_APPLIED or record.target != expected_target:
            return False
        record.phase = ExternalUserQuestionPhase.COMPLETED
        record.updated_at = self._wall_clock()
        return True

    def mark_indeterminate(
        self,
        *,
        tool_use_id: str,
        expected_target: ExternalUserQuestionTarget,
        reason: str,
    ) -> bool:
        record = self._pending.get(tool_use_id)
        if (
            record is None
            or record.phase
            not in {
                ExternalUserQuestionPhase.ACTIVE,
                ExternalUserQuestionPhase.TERMINAL_ACTION_APPLIED,
            }
            or record.target != expected_target
        ):
            return False
        record.phase = ExternalUserQuestionPhase.INDETERMINATE
        record.failure_reason = reason.strip() or "question action result is indeterminate"
        record.updated_at = self._wall_clock()
        return True

    def remove(self, tool_use_id: str) -> PendingExternalUserQuestionSnapshot | None:
        record = self._pending.pop(tool_use_id, None)
        return PendingExternalUserQuestionSnapshot.from_record(record) if record is not None else None

    def remove_if_matches(
        self,
        *,
        tool_use_id: str,
        session_id: str,
        expected_target: ExternalUserQuestionTarget,
    ) -> PendingExternalUserQuestionSnapshot | None:
        record = self._pending.get(tool_use_id)
        if record is None or record.session_id != session_id or record.target != expected_target:
            return None
        self._pending.pop(tool_use_id, None)
        return PendingExternalUserQuestionSnapshot.from_record(record)

    def invalidate_tool(self, tool_use_id: str) -> int:
        return 1 if self._pending.pop(tool_use_id, None) is not None else 0

    def invalidate_session(self, session_id: str) -> int:
        keys = [key for key, pending in self._pending.items() if pending.session_id == session_id]
        for key in keys:
            self._pending.pop(key, None)
        return len(keys)

    def invalidate_binding(self, session_id: str, binding_id: str) -> int:
        keys = [
            key
            for key, pending in self._pending.items()
            if pending.session_id == session_id
            and isinstance(pending.target, ExternalGhosttyQuestionTarget)
            and pending.target.binding_id == binding_id
        ]
        for key in keys:
            self._pending.pop(key, None)
        return len(keys)

    def invalidate_stale_bindings(self, session_id: str, current_binding_id: str) -> int:
        keys = [
            key
            for key, pending in self._pending.items()
            if pending.session_id == session_id
            and isinstance(pending.target, ExternalGhosttyQuestionTarget)
            and pending.target.binding_id != current_binding_id
        ]
        for key in keys:
            self._pending.pop(key, None)
        return len(keys)

    def invalidate_ghostty_target(
        self,
        *,
        session_id: str,
        binding_id: str,
        terminal_id: str,
        paired_tty: str,
        paired_at: datetime,
    ) -> int:
        expected_target = ExternalGhosttyQuestionTarget(
            binding_id=binding_id,
            terminal_id=terminal_id,
            paired_tty=paired_tty,
            paired_at=paired_at,
        )
        keys = [key for key, pending in self._pending.items() if pending.session_id == session_id and pending.target == expected_target]
        for key in keys:
            self._pending.pop(key, None)
        return len(keys)

    def clear(self) -> tuple[PendingExternalUserQuestionSnapshot, ...]:
        removed = tuple(PendingExternalUserQuestionSnapshot.from_record(record) for record in self._pending.values())
        self._pending.clear()
        return removed

    def prune_stale(self) -> tuple[PendingExternalUserQuestionSnapshot, ...]:
        return self._prune_stale()

    def _prune_stale(self) -> tuple[PendingExternalUserQuestionSnapshot, ...]:
        now = self._wall_clock()
        stale_keys: list[str] = []
        for key, pending in self._pending.items():
            age = (now - pending.created_at).total_seconds()
            updated_age = (now - pending.updated_at).total_seconds()
            if pending.phase is ExternalUserQuestionPhase.ACTIVE:
                stale = age > self._ttl_sec
            else:
                stale = updated_age > self._ttl_sec * _NON_ACTIVE_GRACE_MULTIPLIER
            if stale:
                stale_keys.append(key)

        removed = tuple(PendingExternalUserQuestionSnapshot.from_record(self._pending.pop(key)) for key in stale_keys)
        return removed
