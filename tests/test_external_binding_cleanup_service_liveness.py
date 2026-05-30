"""Unit tests for ExternalBindingCleanupService idle-path + startup behavior.

Spec: external-binding-pid-liveness, Task 9.5

These three tests exercise the idle-TTL fallback path and the startup pass
(all with ``liveness_enabled=False``, so liveness governance is bypassed and
the existing idle-TTL behavior is the one under test):

  * Idle path label (Req 8.2) — an idle-TTL removal logs reason
    ``idle_ttl_expired``.
  * Race preservation (Req 7.5) — a ``touch_activity`` that races in between
    the cleanup snapshot and the final removal causes the binding to be
    RETAINED (the race-safe re-read observes the refreshed activity).
  * Startup pass (Req 11.6) — ``start()`` awaits one ``_cleanup()`` before
    creating the periodic task, so a stale binding present before ``start()``
    is gone after ``await start()`` returns.

The removal INFO log is emitted by the reaper logger
``app.services.external_binding_reaper``.

**Validates: Requirements 7.5, 8.2, 11.6**
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_cleanup_service import ExternalBindingCleanupService
from app.services.external_binding_reaper import ExternalBindingReaper
from app.services.external_binding_store import ExternalBindingStore

# The removal INFO log is emitted by the reaper, not the cleanup service.
CLEANUP_LOGGER_NAME = "app.services.external_binding_reaper"
TTL = timedelta(hours=24)
# A long interval so the periodic loop never fires during the startup test —
# the assertion targets only the initial awaited cleanup inside start().
LONG_INTERVAL_SEC = 3600.0


def make_service(
    store: ExternalBindingStore,
    *,
    has_pending: bool = False,
    on_pending_check=None,
    interval_sec: float = 30.0,
) -> tuple[ExternalBindingCleanupService, AsyncMock, AsyncMock]:
    """Build the cleanup service with a real store, a real reaper, and fully
    mocked async collaborators (``auto_approve`` + ``hook_socket_server``).

    ``liveness_enabled`` is fixed to ``False`` so the idle-TTL path is the one
    exercised by these tests.

    ``has_pending``      - constant return for ``has_pending_permission`` when
                           no ``on_pending_check`` is supplied.
    ``on_pending_check`` - optional ``side_effect`` for
                           ``has_pending_permission`` (used to model a race
                           mutating the store between snapshot and removal).
    """
    auto_approve = AsyncMock()
    auto_approve.clear_session = AsyncMock()

    hook_server = AsyncMock()
    if on_pending_check is not None:
        hook_server.has_pending_permission = AsyncMock(side_effect=on_pending_check)
    else:
        hook_server.has_pending_permission = AsyncMock(return_value=has_pending)
    hook_server.cancel_pending_permissions = AsyncMock()

    service = ExternalBindingCleanupService(
        binding_store=store,
        hook_socket_server=hook_server,
        reaper=ExternalBindingReaper(
            binding_store=store,
            auto_approve_service=auto_approve,
            hook_socket_server=hook_server,
        ),
        liveness_enabled=False,
        ttl=TTL,
        interval_sec=interval_sec,
    )
    return service, auto_approve, hook_server


def _save_binding(
    store: ExternalBindingStore,
    *,
    session_id: str,
    user_id: int = 42,
    cwd: str = "/home/user/project",
    age: timedelta,
) -> ExternalBinding:
    """Save a binding whose ``bound_at`` (and default ``last_activity_at``) is
    offset by ``age`` from now.
    """
    binding = ExternalBinding(
        session_id=session_id,
        user_id=user_id,
        cwd=cwd,
        bound_at=utc_now() - age,
        jsonl_path=None,
    )
    store.save_binding(binding)
    return binding


# --- Idle path label (Req 8.2) ----------------------------------------------


async def test_idle_ttl_removal_logs_reason_idle_ttl_expired(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An idle-TTL removal (binding older than TTL, no pending) logs reason
    ``idle_ttl_expired``.

    **Validates: Requirements 8.2**
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="idle-label", age=timedelta(hours=25))

    service, _auto_approve, _hook_server = make_service(store, has_pending=False)

    with caplog.at_level(logging.INFO, logger=CLEANUP_LOGGER_NAME):
        await service._cleanup()

    assert store.get_binding("idle-label") is None, "stale binding should be removed on the idle-TTL path"

    matching = [r for r in caplog.records if getattr(r, "session_id", None) == "idle-label"]
    assert matching, "expected a removal log record for idle-label"
    assert getattr(matching[0], "reason", None) == "idle_ttl_expired"


# --- Race preservation (Req 7.5) --------------------------------------------


async def test_race_touch_activity_between_snapshot_and_removal_retains_binding(
    tmp_path: Path,
) -> None:
    """A ``touch_activity`` racing in between the cleanup snapshot and the
    final removal causes the binding to be RETAINED.

    The ``has_pending_permission`` await is the race window: a hook event
    lands during it and refreshes ``last_activity_at`` to now, so the final
    race-safe re-read sees a fresh binding and skips removal.

    **Validates: Requirements 7.5**
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="race-retain", age=timedelta(hours=30))

    def refresh_then_say_no_pending(*, session_id: str) -> bool:
        # Simulate a hook event landing during the await: activity is
        # refreshed to "now" so idle_age collapses to ~0.
        store.touch_activity(session_id, utc_now())
        return False

    service, auto_approve, hook_server = make_service(
        store,
        on_pending_check=refresh_then_say_no_pending,
    )

    await service._cleanup()

    assert store.get_binding("race-retain") is not None, "binding refreshed during cleanup must be RETAINED by the final re-read"
    # No removal occurred, so the reaper's cleanup side effects never fired.
    auto_approve.clear_session.assert_not_awaited()
    hook_server.cancel_pending_permissions.assert_not_awaited()


# --- Startup pass (Req 11.6) ------------------------------------------------


async def test_start_awaits_initial_cleanup_before_periodic_loop(tmp_path: Path) -> None:
    """``start()`` awaits one ``_cleanup()`` before creating the periodic task.

    A stale binding present before ``start()`` is gone after ``await start()``
    returns. A long interval ensures the periodic loop never fires during the
    test, so the removal is attributable solely to the initial awaited pass.

    **Validates: Requirements 11.6**
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="startup-stale", age=timedelta(hours=48))

    service, auto_approve, hook_server = make_service(
        store,
        has_pending=False,
        interval_sec=LONG_INTERVAL_SEC,
    )

    assert store.get_binding("startup-stale") is not None, "precondition: stale binding present before start()"

    try:
        await service.start()

        assert store.get_binding("startup-stale") is None, "stale binding should be removed by the initial cleanup awaited inside start()"
        auto_approve.clear_session.assert_awaited_once_with("startup-stale")
        hook_server.cancel_pending_permissions.assert_awaited_once_with(session_id="startup-stale")
    finally:
        await service.stop()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
