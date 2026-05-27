from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SessionOrigin(StrEnum):
    OWNED = "owned"
    EXTERNAL_BOUND = "external_bound"
    EXTERNAL_UNBOUND = "external_unbound"


class AuthorizationMode(StrEnum):
    OWNER = "owner"
    BOUND_USER = "bound_user"
    ALLOWED_USERS_SNAPSHOT = "allowed_users_snapshot"
    ALL_USERS = "all_users"
    SOLE_AUTO_APPROVE_USER = "sole_auto_approve_user"


class PermissionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    AUTO_APPROVE = "auto_approve"


class CallbackRecordStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    DISPATCH_FAILED = "dispatch_failed"
    SESSION_ENDED = "session_ended"
    SUPERSEDED = "superseded"


class AutoApproveOutcome(StrEnum):
    APPROVED = "approved"
    NOT_APPROVED = "not_approved"
    APPROVAL_FAILED = "approval_failed"
    APPROVAL_UNKNOWN = "approval_unknown"


@dataclass(slots=True)
class PermissionCallbackRecord:
    token: str
    tool_use_id: str
    session_id: str
    origin: SessionOrigin
    authorization_mode: AuthorizationMode
    authorized_user_ids: frozenset[int]
    created_at: datetime
    expires_at: datetime
    status: CallbackRecordStatus
    decision: PermissionAction | None
    responded_by_user_id: int | None
    responded_at: datetime | None
    dispatch_error_reason: str | None


@dataclass(frozen=True, slots=True)
class PermissionCallbackRecordSnapshot:
    token: str
    tool_use_id: str
    session_id: str
    origin: SessionOrigin
    authorization_mode: AuthorizationMode
    authorized_user_ids: frozenset[int]
    created_at: datetime
    expires_at: datetime
    status: CallbackRecordStatus
    decision: PermissionAction | None
    responded_by_user_id: int | None
    responded_at: datetime | None
    dispatch_error_reason: str | None

    @classmethod
    def from_record(cls, record: PermissionCallbackRecord) -> PermissionCallbackRecordSnapshot:
        return cls(
            token=record.token,
            tool_use_id=record.tool_use_id,
            session_id=record.session_id,
            origin=record.origin,
            authorization_mode=record.authorization_mode,
            authorized_user_ids=record.authorized_user_ids,
            created_at=record.created_at,
            expires_at=record.expires_at,
            status=record.status,
            decision=record.decision,
            responded_by_user_id=record.responded_by_user_id,
            responded_at=record.responded_at,
            dispatch_error_reason=record.dispatch_error_reason,
        )


@dataclass(frozen=True, slots=True)
class ConsumeConsumed:
    snapshot: PermissionCallbackRecordSnapshot


@dataclass(frozen=True, slots=True)
class ConsumeUnauthorized:
    pass


@dataclass(frozen=True, slots=True)
class ConsumeAlreadyResponded:
    pass


@dataclass(frozen=True, slots=True)
class ConsumeDispatchFailed:
    reason: str


@dataclass(frozen=True, slots=True)
class ConsumeNotFound:
    pass


ConsumeResult = ConsumeConsumed | ConsumeUnauthorized | ConsumeAlreadyResponded | ConsumeDispatchFailed | ConsumeNotFound


@dataclass(frozen=True, slots=True)
class PreflightEligible:
    snapshot: PermissionCallbackRecordSnapshot


@dataclass(frozen=True, slots=True)
class PreflightNotUnbound:
    snapshot: PermissionCallbackRecordSnapshot


@dataclass(frozen=True, slots=True)
class PreflightUnauthorized:
    pass


@dataclass(frozen=True, slots=True)
class PreflightAlreadyResponded:
    pass


@dataclass(frozen=True, slots=True)
class PreflightDispatchFailed:
    reason: str


@dataclass(frozen=True, slots=True)
class PreflightNotFound:
    pass


PreflightResult = (
    PreflightEligible
    | PreflightNotUnbound
    | PreflightUnauthorized
    | PreflightAlreadyResponded
    | PreflightDispatchFailed
    | PreflightNotFound
)


class InFlightConflictError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _PermissionCallbackEntry:
    tool_use_id: str
    expires_at: float


class PermissionCallbackRegistry:
    def __init__(
        self,
        *,
        ttl_sec: int,
        token_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")
        self._ttl_sec = ttl_sec
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(6))
        self._clock = clock or time.monotonic
        self._entries: dict[str, _PermissionCallbackEntry] = {}

    def register(self, tool_use_id: str) -> str:
        self._prune_expired()
        for _ in range(16):
            token = self._token_factory()
            if token not in self._entries:
                self._entries[token] = _PermissionCallbackEntry(
                    tool_use_id=tool_use_id,
                    expires_at=self._clock() + self._ttl_sec,
                )
                return token
        raise RuntimeError("failed to generate unique permission callback token")

    def resolve(self, token: str) -> str | None:
        self._prune_expired()
        entry = self._entries.get(token)
        if entry is None:
            return None
        return entry.tool_use_id

    def _prune_expired(self) -> None:
        now = self._clock()
        expired = [token for token, entry in self._entries.items() if entry.expires_at <= now]
        for token in expired:
            self._entries.pop(token, None)

    def __len__(self) -> int:
        """Return the number of live (non-expired) entries."""
        self._prune_expired()
        return len(self._entries)
