"""Shared TTL/token machinery for callback token registries.

``PermissionCallbackRegistry`` and ``PairingCallbackRegistry`` share the same
mechanics: a records dict, monotonic TTL deadlines, a compound index, an
``asyncio.Lock``, grace-period eviction of resolved records and retry-based
unique-token generation. This base class hosts that machinery once; subclasses
only supply domain policy — the record/status types, the pending sentinel via
:meth:`_pending_status`, and their own public API.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

RecordT = TypeVar("RecordT", bound="_ExpirableRecord")
StatusT = TypeVar("StatusT", bound=StrEnum)
CompoundKeyT = TypeVar("CompoundKeyT")


class _ExpirableRecord(Protocol):
    """Minimal record surface the eviction/expiry machinery reads."""

    @property
    def token(self) -> str: ...

    @property
    def expires_at(self) -> datetime: ...

    @property
    def status(self) -> StrEnum: ...


class BaseCallbackRegistry(ABC, Generic[RecordT, StatusT, CompoundKeyT]):
    """In-memory, TTL-bounded token registry core.

    Construct with ``ttl_sec`` (token lifetime), an optional token factory and
    clocks (for tests). All shared state is created here; subclasses must not
    re-initialise it.

      * expiry is judged on a monotonic clock (immune to wall-clock jumps);
        wall-clock stamps only back snapshots and fallback paths;
      * pending records are evicted once expired; non-pending records get a
        grace window (``_NON_PENDING_EVICT_GRACE_MULTIPLIER`` x ttl) so callers
        can still observe a terminal state before reap;
      * tokens are drawn from ``_generate_unique_token`` which retries on
        collision inside ``_records``.
    """

    _NON_PENDING_EVICT_GRACE_MULTIPLIER = 5

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
        self._records: dict[str, RecordT] = {}
        self._ttl_deadlines: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._compound_index: dict[CompoundKeyT, str] = {}

    @abstractmethod
    def _pending_status(self) -> StatusT:
        """Return this registry's status value meaning "awaiting resolution"."""

    def _generate_unique_token(self, error_context: str) -> str:
        """Draw tokens until one is unused; raise after repeated collisions."""
        for _ in range(16):
            token = self._token_factory()
            if token and token not in self._records:
                return token
        raise RuntimeError(f"failed to generate unique {error_context}")

    def _is_expired(self, record: RecordT) -> bool:
        if record.status is not self._pending_status():
            return False
        deadline = self._ttl_deadlines.get(record.token)
        if deadline is not None:
            return deadline <= self._clock()
        return record.expires_at <= self._now_datetime()

    def _evict_stale(self) -> None:
        grace = self._ttl_sec * self._NON_PENDING_EVICT_GRACE_MULTIPLIER
        monotonic_grace_cutoff = self._clock() - grace
        wall_grace_cutoff = self._now_datetime() - timedelta(seconds=grace)
        pending_status = self._pending_status()
        stale_tokens: list[str] = []
        for token, record in self._records.items():
            deadline = self._ttl_deadlines.get(token)
            if record.status is pending_status:
                if self._is_expired(record):
                    stale_tokens.append(token)
            else:
                if deadline is not None:
                    if deadline <= monotonic_grace_cutoff:
                        stale_tokens.append(token)
                elif record.expires_at <= wall_grace_cutoff:
                    stale_tokens.append(token)

        for token in stale_tokens:
            self._records.pop(token, None)
            self._ttl_deadlines.pop(token, None)
        if stale_tokens:
            stale_set = set(stale_tokens)
            for compound_key, token in list(self._compound_index.items()):
                if token in stale_set:
                    self._compound_index.pop(compound_key, None)

    def _now_datetime(self) -> datetime:
        return self._wall_clock()
