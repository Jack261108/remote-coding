from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

logger = logging.getLogger(__name__)


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
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    telegram_message_text: str | None = None


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
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    telegram_message_text: str | None = None

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
            telegram_chat_id=record.telegram_chat_id,
            telegram_message_id=record.telegram_message_id,
            telegram_message_text=record.telegram_message_text,
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


class PermissionCallbackRegistry:
    def __init__(
        self,
        *,
        ttl_sec: int,
        token_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")
        self._ttl_sec = ttl_sec
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(6))
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._records: dict[str, PermissionCallbackRecord] = {}
        self._ttl_deadlines: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._compound_index: dict[tuple[str, str], str] = {}

    async def register_token(
        self,
        tool_use_id: str,
        session_id: str,
        origin: SessionOrigin,
        authorization_mode: AuthorizationMode,
        authorized_user_ids: frozenset[int],
        telegram_chat_id: int | None = None,
        telegram_message_id: int | None = None,
    ) -> str:
        async with self._lock:
            self._evict_stale()
            compound_key = (session_id, tool_use_id)
            existing_token = self._compound_index.get(compound_key)
            existing_record = self._records.get(existing_token) if existing_token is not None else None
            if existing_record is not None and existing_record.status is CallbackRecordStatus.CLAIMED:
                raise InFlightConflictError(f"permission callback already in flight for {session_id}:{tool_use_id}")

            for _ in range(16):
                token = self._token_factory()
                if token in self._records:
                    continue
                monotonic_now = self._clock()
                created_at = self._now_datetime()
                record = PermissionCallbackRecord(
                    token=token,
                    tool_use_id=tool_use_id,
                    session_id=session_id,
                    origin=origin,
                    authorization_mode=authorization_mode,
                    authorized_user_ids=frozenset(authorized_user_ids),
                    created_at=created_at,
                    expires_at=created_at + timedelta(seconds=self._ttl_sec),
                    status=CallbackRecordStatus.PENDING,
                    decision=None,
                    responded_by_user_id=None,
                    responded_at=None,
                    dispatch_error_reason=None,
                    telegram_chat_id=telegram_chat_id,
                    telegram_message_id=telegram_message_id,
                )
                break
            else:
                raise RuntimeError("failed to generate unique permission callback token")

            if existing_record is not None and existing_record.status in {
                CallbackRecordStatus.PENDING,
                CallbackRecordStatus.DISPATCH_FAILED,
            }:
                existing_record.status = CallbackRecordStatus.SUPERSEDED
            self._records[token] = record
            self._ttl_deadlines[token] = monotonic_now + self._ttl_sec
            self._compound_index[compound_key] = token
            return token

    async def consume(self, token: str, user_id: int, action: PermissionAction) -> ConsumeResult:
        async with self._lock:
            self._evict_stale()
            record = self._records.get(token)
            if (
                record is None
                or self._is_expired(record)
                or record.status
                in {
                    CallbackRecordStatus.SESSION_ENDED,
                    CallbackRecordStatus.SUPERSEDED,
                }
            ):
                return ConsumeNotFound()
            if not self._is_authorized(record, user_id):
                return ConsumeUnauthorized()
            if record.status is CallbackRecordStatus.DISPATCH_FAILED:
                return ConsumeDispatchFailed(self._dispatch_failed_reason(record))
            if record.status in {CallbackRecordStatus.CLAIMED, CallbackRecordStatus.RESOLVED}:
                return ConsumeAlreadyResponded()

            record.status = CallbackRecordStatus.CLAIMED
            record.decision = action
            record.responded_by_user_id = user_id
            record.responded_at = self._now_datetime()
            return ConsumeConsumed(PermissionCallbackRecordSnapshot.from_record(record))

    async def inspect_for_auto_approve_preflight(self, token: str, user_id: int) -> PreflightResult:
        async with self._lock:
            record = self._records.get(token)
            if (
                record is None
                or self._is_expired(record)
                or record.status
                in {
                    CallbackRecordStatus.SESSION_ENDED,
                    CallbackRecordStatus.SUPERSEDED,
                }
            ):
                return PreflightNotFound()
            if not self._is_authorized(record, user_id):
                return PreflightUnauthorized()
            if record.status is CallbackRecordStatus.DISPATCH_FAILED:
                return PreflightDispatchFailed(self._dispatch_failed_reason(record))
            if record.status in {CallbackRecordStatus.CLAIMED, CallbackRecordStatus.RESOLVED}:
                return PreflightAlreadyResponded()

            snapshot = PermissionCallbackRecordSnapshot.from_record(record)
            if record.origin is SessionOrigin.EXTERNAL_UNBOUND:
                return PreflightEligible(snapshot)
            return PreflightNotUnbound(snapshot)

    async def mark_resolved(self, token: str) -> bool:
        async with self._lock:
            record = self._records.get(token)
            if record is not None and record.status is CallbackRecordStatus.CLAIMED:
                record.status = CallbackRecordStatus.RESOLVED
                return True
            logger.warning("mark_resolved ignored for token %s with status %s", token, record.status if record is not None else "missing")
            return False

    async def mark_dispatch_failed(self, token: str, reason: str) -> bool:
        async with self._lock:
            record = self._records.get(token)
            if record is not None and record.status is CallbackRecordStatus.CLAIMED:
                record.status = CallbackRecordStatus.DISPATCH_FAILED
                record.dispatch_error_reason = reason
                return True
            logger.warning(
                "mark_dispatch_failed ignored for token %s with status %s", token, record.status if record is not None else "missing"
            )
            return False

    async def invalidate_session(self, session_id: str) -> int:
        async with self._lock:
            self._evict_stale()
            transitioned_tokens: set[str] = set()
            for record in self._records.values():
                if record.session_id == session_id and record.status in {
                    CallbackRecordStatus.PENDING,
                    CallbackRecordStatus.CLAIMED,
                    CallbackRecordStatus.DISPATCH_FAILED,
                }:
                    record.status = CallbackRecordStatus.SESSION_ENDED
                    transitioned_tokens.add(record.token)

            for compound_key, token in list(self._compound_index.items()):
                if token in transitioned_tokens:
                    self._compound_index.pop(compound_key, None)
            return len(transitioned_tokens)

    async def invalidate_pending_for_tool(self, session_id: str, tool_use_id: str, reason: str) -> int:
        """Invalidate pending permission records for a specific tool in a session.

        This is called when a permission is resolved in the terminal (not via Telegram).
        """
        async with self._lock:
            self._evict_stale()
            transitioned_tokens: set[str] = set()
            for record in self._records.values():
                if (
                    record.session_id == session_id
                    and record.tool_use_id == tool_use_id
                    and record.status
                    in {
                        CallbackRecordStatus.PENDING,
                        CallbackRecordStatus.DISPATCH_FAILED,
                    }
                ):
                    record.status = CallbackRecordStatus.RESOLVED
                    record.decision = PermissionAction.ALLOW
                    record.dispatch_error_reason = reason
                    transitioned_tokens.add(record.token)

            for compound_key, token in list(self._compound_index.items()):
                if token in transitioned_tokens:
                    self._compound_index.pop(compound_key, None)
            return len(transitioned_tokens)

    async def update_telegram_message(self, token: str, chat_id: int, message_id: int, message_text: str | None = None) -> bool:
        """Update the Telegram message ID for a permission token."""
        async with self._lock:
            record = self._records.get(token)
            if record is None:
                return False
            record.telegram_chat_id = chat_id
            record.telegram_message_id = message_id
            record.telegram_message_text = message_text
            return True

    async def get_record(self, token: str) -> PermissionCallbackRecordSnapshot | None:
        """Get a permission record by token."""
        async with self._lock:
            record = self._records.get(token)
            if record is None:
                return None
            return PermissionCallbackRecordSnapshot.from_record(record)

    async def get_record_by_tool_use_id(self, session_id: str, tool_use_id: str) -> PermissionCallbackRecordSnapshot | None:
        """Get a permission record by session_id and tool_use_id."""
        async with self._lock:
            for record in self._records.values():
                if record.session_id == session_id and record.tool_use_id == tool_use_id:
                    return PermissionCallbackRecordSnapshot.from_record(record)
            return None

    async def find_pending_for_user(self, user_id: int, *, sort_desc_by_created_at: bool = True) -> list[PermissionCallbackRecordSnapshot]:
        async with self._lock:
            self._evict_stale()
            snapshots = [
                PermissionCallbackRecordSnapshot.from_record(record)
                for record in self._records.values()
                if record.status is CallbackRecordStatus.PENDING and not self._is_expired(record) and self._is_authorized(record, user_id)
            ]
            snapshots.sort(key=lambda snapshot: snapshot.created_at, reverse=sort_desc_by_created_at)
            return snapshots

    def _is_authorized(self, record: PermissionCallbackRecord, user_id: int) -> bool:
        if record.authorization_mode is AuthorizationMode.ALL_USERS:
            return True
        return user_id in record.authorized_user_ids

    def _dispatch_failed_reason(self, record: PermissionCallbackRecord) -> str:
        reason = record.dispatch_error_reason
        if not isinstance(reason, str):
            raise AssertionError("dispatch_failed callback record is missing dispatch_error_reason")
        return reason

    def _is_expired(self, record: PermissionCallbackRecord) -> bool:
        if record.status is not CallbackRecordStatus.PENDING:
            return False
        deadline = self._ttl_deadlines.get(record.token)
        if deadline is not None:
            return deadline <= self._clock()
        return record.expires_at <= self._now_datetime()

    def _prune_expired(self) -> set[str]:
        return {token for token, record in self._records.items() if self._is_expired(record)}

    def _evict_stale(self) -> None:
        monotonic_stale_cutoff = self._clock() - self._ttl_sec
        wall_stale_cutoff = self._now_datetime() - timedelta(seconds=self._ttl_sec)
        stale_tokens = []
        for token, record in self._records.items():
            if record.status is not CallbackRecordStatus.PENDING:
                continue
            deadline = self._ttl_deadlines.get(token)
            if deadline is not None:
                if deadline <= monotonic_stale_cutoff:
                    stale_tokens.append(token)
            elif record.expires_at <= wall_stale_cutoff:
                stale_tokens.append(token)

        for token in stale_tokens:
            self._records.pop(token, None)
            self._ttl_deadlines.pop(token, None)
        if stale_tokens:
            stale_token_set = set(stale_tokens)
            for compound_key, token in list(self._compound_index.items()):
                if token in stale_token_set:
                    self._compound_index.pop(compound_key, None)

    def _now_datetime(self) -> datetime:
        return self._wall_clock()
