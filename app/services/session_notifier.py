"""Session notification service for pub/sub revision tracking."""

from __future__ import annotations

import asyncio

from app.domain.session_models import SessionState


class SessionNotifier:
    """Manages revision counters and asyncio.Condition-based pub/sub.

    Tracks a per-session cursor that increments on each publish() call,
    and provides async wait methods for consumers to block until a new
    revision is available.
    """

    def __init__(self) -> None:
        self._cursors: dict[str, int] = {}
        self._revision_conditions: dict[str, asyncio.Condition] = {}

    def publish(self, session_id: str, state: SessionState) -> None:
        """Increment the revision cursor for *session_id* and notify waiters.

        The state argument is accepted for interface consistency but the
        notifier tracks cursors independently of SessionState.revision.
        """
        self._cursors[session_id] = self._cursors.get(session_id, 0) + 1

        condition = self._revision_conditions.get(session_id)
        if condition is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _notify() -> None:
            async with condition:
                condition.notify_all()

        loop.create_task(_notify())

    def get_cursor(self, session_id: str) -> int:
        """Return the current revision cursor for *session_id* (0 if unknown)."""
        return self._cursors.get(session_id, 0)

    async def wait_for_publish(self, session_id: str, *, since_cursor: int, timeout_sec: float) -> bool:
        """Block until the cursor for *session_id* exceeds *since_cursor*.

        Returns True if a new revision was observed, False on timeout.
        """
        if self.get_cursor(session_id) > since_cursor:
            return True

        condition = self._revision_conditions.setdefault(session_id, asyncio.Condition())
        async with condition:
            if self.get_cursor(session_id) > since_cursor:
                return True
            try:
                await asyncio.wait_for(
                    condition.wait_for(lambda: self.get_cursor(session_id) > since_cursor),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                return False
        return True

    async def wait_for_change(self, session_id: str, *, since_revision: int, timeout_sec: float) -> bool:
        """Alias for wait_for_publish using revision terminology."""
        return await self.wait_for_publish(session_id, since_cursor=since_revision, timeout_sec=timeout_sec)
