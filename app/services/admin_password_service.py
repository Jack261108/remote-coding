"""Admin password verification for accessing directories outside the allowed whitelist."""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.domain.models import utc_now

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SEC = 120.0
_MAX_ATTEMPTS = 10


class VerifyResult(StrEnum):
    """Outcome of a password verification attempt."""

    VERIFIED = "verified"
    WRONG_PASSWORD = "wrong_password"
    MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
    NO_CHALLENGE = "no_challenge"


@dataclass(slots=True)
class VerifyOutcome:
    """Result returned by verify(), carrying both status and the challenge (if verified)."""

    result: VerifyResult
    challenge: PendingPasswordChallenge | None = None


@dataclass(slots=True)
class PendingPasswordChallenge:
    user_id: int
    workdir: str
    action: str
    provider: str | None = None
    attempts: int = 0
    created_at: datetime = field(default_factory=utc_now)


class AdminPasswordService:
    """In-memory store for pending admin password challenges."""

    def __init__(self, password: str, *, ttl_sec: float = _DEFAULT_TTL_SEC, max_attempts: int = _MAX_ATTEMPTS) -> None:
        self._password = password
        self._ttl_sec = ttl_sec
        self._max_attempts = max_attempts
        self._pending: dict[int, PendingPasswordChallenge] = {}

    @property
    def is_enabled(self) -> bool:
        return bool(self._password)

    def start_challenge(self, user_id: int, workdir: str, action: str, *, provider: str | None = None) -> bool:
        """Start a password challenge. Returns True if started, False if one is already pending."""
        self._prune_stale()
        if user_id in self._pending:
            return False
        self._pending[user_id] = PendingPasswordChallenge(
            user_id=user_id,
            workdir=workdir,
            action=action,
            provider=provider,
        )
        logger.info("admin password challenge started", extra={"user_id": user_id, "workdir": workdir, "action": action})
        return True

    def has_pending(self, user_id: int) -> bool:
        self._prune_stale()
        return user_id in self._pending

    def verify(self, user_id: int, password: str) -> VerifyOutcome:
        """Verify password and return a discriminated outcome.

        Returns VerifyOutcome with one of:
        - VERIFIED + challenge: password correct, challenge consumed.
        - WRONG_PASSWORD: password wrong, challenge kept for retry.
        - MAX_ATTEMPTS_EXCEEDED: attempts exhausted, challenge removed.
        - NO_CHALLENGE: no pending challenge or it expired.
        """
        self._prune_stale()
        challenge = self._pending.get(user_id)
        if challenge is None:
            return VerifyOutcome(result=VerifyResult.NO_CHALLENGE)
        if challenge.attempts >= self._max_attempts:
            self._pending.pop(user_id)
            logger.warning("admin password max attempts exceeded", extra={"user_id": user_id})
            return VerifyOutcome(result=VerifyResult.MAX_ATTEMPTS_EXCEEDED)
        challenge.attempts += 1
        if not hmac.compare_digest(password, self._password):
            logger.warning("admin password verification failed", extra={"user_id": user_id, "attempt": challenge.attempts})
            return VerifyOutcome(result=VerifyResult.WRONG_PASSWORD)
        self._pending.pop(user_id)
        logger.info("admin password verification succeeded", extra={"user_id": user_id, "workdir": challenge.workdir})
        return VerifyOutcome(result=VerifyResult.VERIFIED, challenge=challenge)

    def cancel(self, user_id: int) -> PendingPasswordChallenge | None:
        self._prune_stale()
        return self._pending.pop(user_id, None)

    def _prune_stale(self) -> None:
        now = utc_now()
        stale_ids = [uid for uid, ch in self._pending.items() if (now - ch.created_at).total_seconds() > self._ttl_sec]
        for uid in stale_ids:
            self._pending.pop(uid, None)
