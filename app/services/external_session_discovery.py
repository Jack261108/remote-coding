from __future__ import annotations

import logging
from collections.abc import Callable

from app.domain.external_session_models import UnboundExternalSession
from app.domain.hook_models import HookEvent
from app.domain.models import utc_now
from app.services.process_liveness import process_is_alive

logger = logging.getLogger(__name__)


class ExternalSessionDiscoveryService:
    """Tracks unbound external sessions discovered via hook events."""

    def __init__(
        self,
        *,
        stale_timeout_sec: float = 600.0,
        title_resolver: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._stale_timeout_sec = stale_timeout_sec
        self._title_resolver = title_resolver
        self._sessions: dict[str, UnboundExternalSession] = {}

    def record_event(self, event: HookEvent) -> None:
        """Record a hook event from an unbound session.

        Creates a new entry if session_id not yet tracked, otherwise updates
        last_seen and increments event_count.
        """
        now = utc_now()
        existing = self._sessions.get(event.session_id)
        if existing is None:
            title = self._resolve_title(event.session_id, event.cwd)
            self._sessions[event.session_id] = UnboundExternalSession(
                session_id=event.session_id,
                cwd=event.cwd,
                pid=event.pid,
                first_seen=now,
                last_seen=now,
                event_count=1,
                title=title,
            )
        else:
            existing.last_seen = now
            existing.event_count += 1
            existing.cwd = event.cwd
            if event.pid is not None:
                existing.pid = event.pid
            if existing.title is None:
                existing.title = self._resolve_title(event.session_id, event.cwd)

    def _resolve_title(self, session_id: str, cwd: str) -> str | None:
        """Attempt to resolve session title via the injected resolver."""
        if self._title_resolver is None:
            return None
        try:
            return self._title_resolver(session_id, cwd)
        except Exception:
            logger.debug("title resolver failed", extra={"session_id": session_id})
            return None

    def remove_session(self, session_id: str) -> None:
        """Remove a session from unbound tracking."""
        self._sessions.pop(session_id, None)

    def list_unbound(self) -> list[UnboundExternalSession]:
        """Return all currently-active unbound sessions without pruning."""
        return list(self._sessions.values())

    def _prune_dead(self) -> None:
        """Remove sessions whose pid is no longer running."""
        dead_ids: list[str] = []
        for session_id, session in self._sessions.items():
            if session.pid is not None and not self._is_pid_alive(session.pid):
                dead_ids.append(session_id)
        for session_id in dead_ids:
            del self._sessions[session_id]

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a process is still running."""
        return process_is_alive(pid)

    def get(self, session_id: str) -> UnboundExternalSession | None:
        """Get a specific unbound session by ID."""
        return self._sessions.get(session_id)

    def prune_stale(self) -> list[str]:
        """Remove sessions whose last_seen exceeds stale_timeout_sec.

        Returns the list of removed session IDs.
        """
        now = utc_now()
        stale_ids: list[str] = []
        for session_id, session in self._sessions.items():
            elapsed = (now - session.last_seen).total_seconds()
            if elapsed > self._stale_timeout_sec:
                stale_ids.append(session_id)
        for session_id in stale_ids:
            del self._sessions[session_id]
        return stale_ids

    def count_stale(self) -> int:
        """Count sessions that would be pruned by prune_stale() without removing them."""
        now = utc_now()
        count = 0
        for session in self._sessions.values():
            elapsed = (now - session.last_seen).total_seconds()
            if elapsed > self._stale_timeout_sec:
                count += 1
        return count

    def is_session_stale(self, session_id: str) -> bool:
        """Check if a specific session is stale without removing it."""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        now = utc_now()
        elapsed = (now - session.last_seen).total_seconds()
        return elapsed > self._stale_timeout_sec
