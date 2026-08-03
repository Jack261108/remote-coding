"""Per-user external input target (input-mode state).

Tracks which bound external session a Telegram user is currently driving from
``/list``. Purely in-process and not persisted: restart clears it, but the
persisted ``GhosttyInputTarget`` on the binding survives, so the user only
needs to re-select from ``/list`` (no re-pairing) after a restart.

Concurrency: the input service performs the actual serialised sending under a
per-session ``RefCountedLockRegistry`` lock. This store only guards its own
dict against concurrent asyncio modification with an ``asyncio.Lock``; it does
not enforce send serialisation.

Security anchors (from the design §5):
  * keyed by ``user_id`` — one input target per user, replacing a prior target
    clears the old intent (avoids routing ambiguity across bound sessions);
  * each ``ActiveExternalInputTarget`` records the ``binding_id`` captured at
    selection time, so a later unbind+rebind (new ``binding_id``) invalidates
    a stale intent via ``invalidate_for_binding_aba``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class ActiveExternalInputTarget:
    """The external session a user is currently typing into via Telegram.

    ``binding_id`` is captured at selection time and acts as an ABA guard: the
    input service re-checks it against the live ``ExternalBinding.binding_id``
    on every send/select.
    """

    user_id: int
    session_id: str
    binding_id: str
    selected_at: datetime


class ExternalInputTargetStore:
    """In-memory map of user_id -> active external input target."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._by_user: dict[int, ActiveExternalInputTarget] = {}
        self._lock = asyncio.Lock()
        self._now = now or (lambda: datetime.now(UTC))

    async def set_target(
        self,
        *,
        user_id: int,
        session_id: str,
        binding_id: str,
    ) -> ActiveExternalInputTarget:
        """Set (or replace) the user's current input target.

        Replacing supersedes any prior target for that user — selecting a
        different bound session cleanly switches intent.
        """
        async with self._lock:
            target = ActiveExternalInputTarget(
                user_id=user_id,
                session_id=session_id,
                binding_id=binding_id,
                selected_at=self._now(),
            )
            self._by_user[user_id] = target
            return target

    async def get_target(self, user_id: int) -> ActiveExternalInputTarget | None:
        async with self._lock:
            return self._by_user.get(user_id)

    async def clear_target(self, user_id: int) -> ActiveExternalInputTarget | None:
        """Clear the user's current target (leaving input mode). Returns the
        cleared target, or None if the user had none."""
        async with self._lock:
            return self._by_user.pop(user_id, None)

    async def clear_target_for_session(self, session_id: str) -> list[ActiveExternalInputTarget]:
        """Clear any targets pointing at ``session_id`` (unbind / SessionEnd /
        target failure). Different users may have selected the same session,
        so we clear all of them and return the list for notification."""
        async with self._lock:
            removed = [target for target in self._by_user.values() if target.session_id == session_id]
            for target in removed:
                self._by_user.pop(target.user_id, None)
            return removed

    async def invalidate_for_binding_aba(self, session_id: str, binding_id: str) -> list[ActiveExternalInputTarget]:
        """Drop targets whose ``binding_id`` no longer matches the live binding.

        Called after an unbind+rebind that produced a new ``binding_id``: the
        old intent must not drive the new binding. Returns the invalidated
        targets (for notification). Targets pointing at *other* sessions are
        untouched; a target on ``session_id`` whose ``binding_id`` still
        matches (e.g. only an unrelated binding changed) is left alone.
        """
        async with self._lock:
            removed = [target for target in self._by_user.values() if target.session_id == session_id and target.binding_id != binding_id]
            for target in removed:
                self._by_user.pop(target.user_id, None)
            return removed

    async def all_targets(self) -> list[ActiveExternalInputTarget]:
        """Snapshot of all active targets (diagnostics / tests)."""
        async with self._lock:
            return list(self._by_user.values())
