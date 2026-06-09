from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

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
        self._ended_session_ids: set[str] = set()
        self._unavailable_session_ids: set[str] = set()

    def record_event(self, event: HookEvent) -> None:
        """Record a hook event from an unbound session.

        Creates a new entry if session_id not yet tracked, otherwise updates
        last_seen and increments event_count.
        """
        if event.session_id in self._ended_session_ids:
            return
        now = utc_now()
        existing = self._sessions.get(event.session_id)
        if existing is None:
            self._unavailable_session_ids.discard(event.session_id)
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
            if event.pid is not None and event.pid > 0:
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

    def mark_session_ended(self, session_id: str) -> None:
        """Remember an ended external session so late hooks cannot rediscover it."""
        self.remove_session(session_id)
        self._ended_session_ids.add(session_id)

    def is_session_ended(self, session_id: str) -> bool:
        """Return whether an external session has been marked ended/reaped."""
        return session_id in self._ended_session_ids

    def mark_session_unavailable(self, session_id: str) -> None:
        """Remember a removed external session so old callbacks can report it as unavailable."""
        self.remove_session(session_id)
        self._unavailable_session_ids.add(session_id)

    def unavailable_session_ids(self) -> set[str]:
        """Return IDs removed from discovery but still relevant for old callbacks."""
        return set(self._ended_session_ids | self._unavailable_session_ids)

    def is_session_unavailable(self, session_id: str) -> bool:
        """Return whether a full session ID is unavailable for old callbacks."""
        return session_id in self._ended_session_ids or session_id in self._unavailable_session_ids

    def has_unavailable_session_prefix(self, session_id_prefix: str) -> bool:
        """Return whether a callback prefix points at an unavailable external session."""
        prefix = session_id_prefix
        return any(session_id == prefix or session_id.startswith(prefix) for session_id in self.unavailable_session_ids())

    def list_unbound(self) -> list[UnboundExternalSession]:
        """Return all currently-active unbound sessions without pruning."""
        return list(self._sessions.values())

    def prune_dead(self) -> list[str]:
        """Remove ended sessions whose pid is no longer running."""
        dead_ids: list[str] = []
        for session_id, session in self._sessions.items():
            if session.pid is None or session.pid <= 0:
                continue
            try:
                is_alive = self._is_pid_alive(session.pid)
            except Exception:
                logger.exception("failed to check external session pid", extra={"session_id": session_id, "pid": session.pid})
                continue
            if not is_alive:
                dead_ids.append(session_id)
        for session_id in dead_ids:
            self.mark_session_ended(session_id)
        return dead_ids

    def _prune_dead(self) -> None:
        """Remove sessions whose pid is no longer running."""
        self.prune_dead()

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a process is still running."""
        return process_is_alive(pid)

    def get(self, session_id: str) -> UnboundExternalSession | None:
        """Get a specific unbound session by ID."""
        return self._sessions.get(session_id)

    def _is_stale(self, session: UnboundExternalSession, *, now: datetime) -> bool:
        elapsed = (now - session.last_seen).total_seconds()
        return elapsed > self._stale_timeout_sec

    def prune_stale(self) -> list[str]:
        """Remove sessions whose last_seen exceeds stale_timeout_sec.

        Returns the list of removed session IDs.
        """
        now = utc_now()
        stale_ids = [session_id for session_id, session in self._sessions.items() if self._is_stale(session, now=now)]
        for session_id in stale_ids:
            self.mark_session_unavailable(session_id)
        return stale_ids

    def count_stale(self) -> int:
        """Count sessions that would be pruned by prune_stale() without removing them."""
        now = utc_now()
        return sum(1 for session in self._sessions.values() if self._is_stale(session, now=now))

    def is_session_stale(self, session_id: str) -> bool:
        """Check if a specific session is stale without removing it."""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        return self._is_stale(session, now=utc_now())
