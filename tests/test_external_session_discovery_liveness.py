"""Discovery probe-delegation and Req 11.7 regression tests.

Covers the Requirement 1.8 refactor of
``ExternalSessionDiscoveryService._is_pid_alive`` to delegate to the shared
``process_is_alive`` probe, plus the two behavioral-change regressions called
out in Requirement 11.7:

  (a) a session whose pid probe raises ``PermissionError`` is now treated as
      ALIVE and RETAINED by ``_prune_dead`` (reversing the pre-feature pruning,
      where ``PermissionError`` was treated as dead); and
  (b) the same foreign-owned session is still bounded — ``prune_stale`` removes
      it once its ``last_seen`` exceeds ``stale_timeout_sec`` (default 600s),
      the existing safety net.

Validates: Requirements 1.8, 11.7
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from app.domain.hook_models import HookEvent
from app.domain.models import utc_now
from app.services.external_session_discovery import ExternalSessionDiscoveryService


def _make_hook_event(session_id: str, *, pid: int | None) -> HookEvent:
    """Build a minimal valid HookEvent carrying the given pid."""
    return HookEvent(
        session_id=session_id,
        cwd="/home/user/project",
        event="PreToolUse",
        status="running",
        pid=pid,
    )


def test_is_pid_alive_delegates_to_shared_probe() -> None:
    """``_is_pid_alive`` returns exactly what ``process_is_alive`` returns.

    Per Requirement 1.8, the static method delegates to the single shared
    probe. We patch the discovery-module-bound ``process_is_alive`` (the name
    discovery imported via ``from app.services.process_liveness import
    process_is_alive``) and assert ``_is_pid_alive`` mirrors its result for
    representative outcomes — including a sentinel object — proving it is a
    pure pass-through with no independent logic.

    Validates: Requirements 1.8
    """
    pid = 4242

    # Representative case: probe reports alive.
    with patch(
        "app.services.external_session_discovery.process_is_alive",
        return_value=True,
    ) as probe:
        assert ExternalSessionDiscoveryService._is_pid_alive(pid) == probe(pid)
        assert ExternalSessionDiscoveryService._is_pid_alive(pid) is True

    # Representative case: probe reports dead.
    with patch(
        "app.services.external_session_discovery.process_is_alive",
        return_value=False,
    ) as probe:
        assert ExternalSessionDiscoveryService._is_pid_alive(pid) == probe(pid)
        assert ExternalSessionDiscoveryService._is_pid_alive(pid) is False

    # Sentinel pass-through: whatever the shared probe returns is returned
    # verbatim, confirming there is no independent re-implementation.
    sentinel = object()
    with patch(
        "app.services.external_session_discovery.process_is_alive",
        return_value=sentinel,
    ):
        assert ExternalSessionDiscoveryService._is_pid_alive(pid) is sentinel


def test_prune_dead_retains_pid_zero_unknown_session() -> None:
    """A pid=0 session is RETAINED because pid must be positive to prove liveness."""
    service = ExternalSessionDiscoveryService()
    session_id = "pid-zero-session"

    service.record_event(_make_hook_event(session_id, pid=0))
    assert service.get(session_id) is not None

    with patch("app.services.external_session_discovery.process_is_alive", return_value=False) as probe:
        service._prune_dead()

    probe.assert_not_called()
    assert service.get(session_id) is not None


def test_record_event_does_not_overwrite_positive_pid_with_zero() -> None:
    """A pid=0 refresh keeps the last known positive pid for dead pruning."""
    service = ExternalSessionDiscoveryService()
    session_id = "pid-refresh-session"

    service.record_event(_make_hook_event(session_id, pid=4242))
    service.record_event(_make_hook_event(session_id, pid=0))

    session = service.get(session_id)
    assert session is not None
    assert session.pid == 4242


def test_prune_dead_removes_confirmed_dead_sessions_even_when_later_probe_fails() -> None:
    """A bad pid probe does not keep earlier confirmed-dead sessions around."""
    service = ExternalSessionDiscoveryService()
    dead_id = "dead-before-bad-pid"
    bad_id = "bad-pid-session"

    service.record_event(_make_hook_event(dead_id, pid=4242))
    service.record_event(_make_hook_event(bad_id, pid=2**100))

    def fake_process_is_alive(pid: int) -> bool:
        if pid == 4242:
            return False
        raise OverflowError("bad pid")

    with patch("app.services.external_session_discovery.process_is_alive", side_effect=fake_process_is_alive):
        service._prune_dead()

    assert service.get(dead_id) is None
    assert service.get(bad_id) is not None


def test_prune_dead_retains_permission_error_session() -> None:
    """A ``PermissionError``-owning session is RETAINED by ``_prune_dead``.

    Regression (a) for Requirement 11.7: after the Req 1.8 refactor, a pid
    owned by another user (whose probe raises ``PermissionError``) is treated
    as ALIVE, so ``_prune_dead`` no longer removes it. We exercise the full
    delegation chain by patching ``os.kill`` at
    ``app.services.process_liveness.os.kill`` to raise ``PermissionError``, so
    the real ``process_is_alive`` returns ``True`` and the session survives.

    Validates: Requirements 11.7
    """
    service = ExternalSessionDiscoveryService()
    session_id = "perm-error-session"

    # Register the session with a positive pid; last_seen is fresh (now), so
    # prune_stale would not remove it — isolating _prune_dead behavior.
    service.record_event(_make_hook_event(session_id, pid=4242))
    assert service.get(session_id) is not None

    with patch(
        "app.services.process_liveness.os.kill",
        side_effect=PermissionError(),
    ):
        service._prune_dead()

    # PermissionError -> alive -> retained (reverses pre-feature pruning).
    assert service.get(session_id) is not None


def test_prune_stale_removes_foreign_owned_session_when_last_seen_old() -> None:
    """``prune_stale`` removes the foreign-owned session once it is stale.

    Regression (b) for Requirement 11.7: the ``PermissionError``-owning session
    retained by ``_prune_dead`` is still bounded — once its ``last_seen``
    exceeds ``stale_timeout_sec`` (default 600s), ``prune_stale`` removes it.
    The pid-alive state is irrelevant to ``prune_stale``, so no probe patching
    is needed; we call ``prune_stale`` directly to confirm it (not
    ``_prune_dead``) performs the removal.

    Validates: Requirements 11.7
    """
    service = ExternalSessionDiscoveryService(stale_timeout_sec=600)
    session_id = "stale-foreign-session"

    service.record_event(_make_hook_event(session_id, pid=4242))
    assert service.get(session_id) is not None

    # Age the session past the stale timeout.
    service._sessions[session_id].last_seen = utc_now() - timedelta(seconds=service._stale_timeout_sec + 10)

    removed = service.prune_stale()

    assert session_id in removed
    assert service.get(session_id) is None
