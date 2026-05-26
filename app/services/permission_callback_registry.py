from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


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
