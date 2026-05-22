from __future__ import annotations

import os

from app.domain.external_session_models import UnboundExternalSession
from app.domain.hook_models import HookEvent
from app.domain.models import utc_now


class ExternalSessionDiscoveryService:
    """Tracks unbound external sessions discovered via hook events."""

    def __init__(self, *, stale_timeout_sec: float = 600.0) -> None:
        self._stale_timeout_sec = stale_timeout_sec
        self._sessions: dict[str, UnboundExternalSession] = {}

    def record_event(self, event: HookEvent) -> None:
        """Record a hook event from an unbound session.

        Creates a new entry if session_id not yet tracked, otherwise updates
        last_seen and increments event_count.
        """
        now = utc_now()
        existing = self._sessions.get(event.session_id)
        if existing is None:
            self._sessions[event.session_id] = UnboundExternalSession(
                session_id=event.session_id,
                cwd=event.cwd,
                pid=event.pid,
                first_seen=now,
                last_seen=now,
                event_count=1,
            )
        else:
            existing.last_seen = now
            existing.event_count += 1
            existing.cwd = event.cwd
            if event.pid is not None:
                existing.pid = event.pid

    def remove_session(self, session_id: str) -> None:
        """Remove a session from unbound tracking."""
        self._sessions.pop(session_id, None)

    def list_unbound(self) -> list[UnboundExternalSession]:
        """Return all currently-active unbound sessions (pruning stale/dead ones first)."""
        self._prune_dead()
        self.prune_stale()
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
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False

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
