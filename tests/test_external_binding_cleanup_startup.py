"""Startup and independence tests for ExternalBindingCleanupService.

Spec: stale-external-binding-cleanup (bugfix), Task 8.5
Test plan: T14, T15, T16, T17

These tests cover two related guarantees:

  * **Startup behavior (T14, T15)** — `await service.start()` runs an initial
    cleanup pass synchronously before returning, so any caller (e.g. a `/list`
    handler) running after `start()` cannot observe stale bindings persisted
    from a previous process lifetime; fresh bindings within TTL survive the
    start/stop cycle untouched.
  * **Independence from tmux cleanup (T16, T17)** — the service has no
    dependency on `TmuxRunner` or `SessionRegistryService`; it can be
    instantiated and used in isolation, and a `_cleanup()` pass with no tmux
    infrastructure present does not raise.

**Validates: Requirements 2.11, 4.1, 4.8**
"""

from __future__ import annotations

import inspect
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_cleanup_service import ExternalBindingCleanupService
from app.services.external_binding_reaper import ExternalBindingReaper
from app.services.external_binding_store import ExternalBindingStore

TTL = timedelta(hours=24)
# A long interval so the periodic loop never gets a chance to run during these
# tests — the assertions all target the initial awaited cleanup inside start().
LONG_INTERVAL_SEC = 3600.0


def _make_hook_server(*, has_pending: bool = False) -> AsyncMock:
    hook_server = AsyncMock()
    hook_server.has_pending_permission = AsyncMock(return_value=has_pending)
    hook_server.cancel_pending_permissions = AsyncMock(return_value=None)
    return hook_server


def _make_auto_approve_service() -> AsyncMock:
    auto_approve = AsyncMock()
    auto_approve.clear_session = AsyncMock(return_value=None)
    return auto_approve


def _write_binding_json(
    data_dir: Path,
    *,
    session_id: str,
    user_id: int,
    cwd: str,
    bound_at_iso: str,
    last_activity_at_iso: str | None = None,
    jsonl_path: str | None = None,
) -> None:
    """Pre-write external_bindings.json so a fresh store loads from disk."""
    entry: dict[str, object] = {
        "user_id": user_id,
        "cwd": cwd,
        "bound_at": bound_at_iso,
        "jsonl_path": jsonl_path,
    }
    if last_activity_at_iso is not None:
        entry["last_activity_at"] = last_activity_at_iso
    payload = {session_id: entry}
    (data_dir / "external_bindings.json").write_text(json.dumps(payload), encoding="utf-8")


# --- T14: Startup with stale bindings → cleaned before bot polling ----------


async def test_t14_startup_with_stale_binding_in_json_is_cleaned_after_start(
    tmp_path: Path,
) -> None:
    """T14: stale binding pre-persisted in JSON is gone after `await start()` returns.

    The initial cleanup is awaited inside `start()` itself, so the very first
    caller after `start()` (e.g. `/list`) cannot see a stale binding from a
    prior process lifetime.

    **Validates: Requirements 2.11, 4.8**
    """
    session_id = "stale-startup-1"
    stale_bound_at = utc_now() - timedelta(hours=48)
    _write_binding_json(
        tmp_path,
        session_id=session_id,
        user_id=42,
        cwd="/home/user/project",
        bound_at_iso=stale_bound_at.isoformat(),
    )

    # Fresh store instance loads the persisted (stale) binding from disk —
    # this simulates the process having just restarted.
    store = ExternalBindingStore(data_dir=tmp_path)
    assert store.get_binding(session_id) is not None, "precondition: stale binding loaded from JSON"

    hook_server = _make_hook_server(has_pending=False)
    auto_approve = _make_auto_approve_service()
    reaper = ExternalBindingReaper(
        binding_store=store,
        auto_approve_service=auto_approve,
        hook_socket_server=hook_server,
    )
    service = ExternalBindingCleanupService(
        binding_store=store,
        hook_socket_server=hook_server,
        reaper=reaper,
        liveness_enabled=False,
        ttl=TTL,
        interval_sec=LONG_INTERVAL_SEC,
    )

    try:
        await service.start()

        # The key guarantee: AFTER start() returns the stale binding is gone.
        # /list would not see it because cleanup was awaited inside start().
        assert store.get_binding(session_id) is None, "stale binding should be removed by the initial cleanup awaited in start()"
        # Associated state is cleaned up too.
        auto_approve.clear_session.assert_awaited_once_with(session_id)
        hook_server.cancel_pending_permissions.assert_awaited_once_with(
            session_id=session_id,
        )
    finally:
        await service.stop()


# --- T15: Startup with fresh bindings → preserved ---------------------------


async def test_t15_startup_with_fresh_binding_in_json_is_preserved(tmp_path: Path) -> None:
    """T15: a binding within the TTL window survives a start/stop cycle.

    The initial cleanup pass must not touch fresh bindings.

    **Validates: Requirements 2.11, 4.8**
    """
    session_id = "fresh-startup-1"
    # 1h old — well inside the 24h TTL.
    fresh_ts = utc_now() - timedelta(hours=1)
    _write_binding_json(
        tmp_path,
        session_id=session_id,
        user_id=7,
        cwd="/home/user/other",
        bound_at_iso=fresh_ts.isoformat(),
        last_activity_at_iso=fresh_ts.isoformat(),
    )

    store = ExternalBindingStore(data_dir=tmp_path)
    assert store.get_binding(session_id) is not None, "precondition: fresh binding loaded from JSON"

    hook_server = _make_hook_server(has_pending=False)
    auto_approve = _make_auto_approve_service()
    reaper = ExternalBindingReaper(
        binding_store=store,
        auto_approve_service=auto_approve,
        hook_socket_server=hook_server,
    )
    service = ExternalBindingCleanupService(
        binding_store=store,
        hook_socket_server=hook_server,
        reaper=reaper,
        liveness_enabled=False,
        ttl=TTL,
        interval_sec=LONG_INTERVAL_SEC,
    )

    try:
        await service.start()

        retained = store.get_binding(session_id)
        assert retained is not None, "fresh binding should be preserved by startup cleanup"
        assert retained.user_id == 7
        assert retained.cwd == "/home/user/other"
        # No removal-side-effects fired.
        auto_approve.clear_session.assert_not_called()
        hook_server.cancel_pending_permissions.assert_not_called()
    finally:
        await service.stop()


# --- T16: External cleanup operates independently of SessionRegistry --------


def test_t16_cleanup_service_constructor_has_no_tmux_or_session_registry_dependency() -> None:
    """T16 (structural): the service's constructor takes neither a tmux runner
    nor a session registry. This documents the architectural decoupling.

    **Validates: Requirements 4.1**
    """
    sig = inspect.signature(ExternalBindingCleanupService.__init__)
    params = set(sig.parameters.keys())

    forbidden = {
        "tmux_runner",
        "session_registry",
        "session_registry_service",
        "registry",
    }
    overlap = params & forbidden
    assert not overlap, f"ExternalBindingCleanupService must not depend on tmux/registry, " f"but constructor accepts: {overlap}"

    # Sanity: the dependencies it DOES need are present.
    expected = {"binding_store", "hook_socket_server", "reaper", "liveness_enabled", "ttl", "interval_sec"}
    missing = expected - params
    assert not missing, f"constructor missing expected params: {missing}"


async def test_t16_cleanup_runs_standalone_cleans_stale_preserves_fresh(tmp_path: Path) -> None:
    """T16 (behavioral): instantiate the service alone (no SessionRegistry, no
    TmuxRunner) and confirm it correctly cleans stale and preserves fresh
    bindings end-to-end.

    **Validates: Requirements 4.1**
    """
    store = ExternalBindingStore(data_dir=tmp_path)

    stale_id = "stale-standalone"
    fresh_id = "fresh-standalone"
    store.save_binding(
        ExternalBinding(
            session_id=stale_id,
            user_id=1,
            cwd="/home/user/stale",
            bound_at=utc_now() - timedelta(hours=48),
            jsonl_path=None,
        )
    )
    store.save_binding(
        ExternalBinding(
            session_id=fresh_id,
            user_id=2,
            cwd="/home/user/fresh",
            bound_at=utc_now() - timedelta(minutes=5),
            jsonl_path=None,
        )
    )

    hook_server = _make_hook_server(has_pending=False)
    auto_approve = _make_auto_approve_service()
    reaper = ExternalBindingReaper(
        binding_store=store,
        auto_approve_service=auto_approve,
        hook_socket_server=hook_server,
    )
    service = ExternalBindingCleanupService(
        binding_store=store,
        hook_socket_server=hook_server,
        reaper=reaper,
        liveness_enabled=False,
        ttl=TTL,
        interval_sec=LONG_INTERVAL_SEC,
    )

    try:
        await service.start()

        assert store.get_binding(stale_id) is None, "stale binding should be removed"
        assert store.get_binding(fresh_id) is not None, "fresh binding should be preserved"

        # Cleanup side effects fired only for the stale one.
        auto_approve.clear_session.assert_awaited_once_with(stale_id)
        hook_server.cancel_pending_permissions.assert_awaited_once_with(session_id=stale_id)
    finally:
        await service.stop()


# --- T17: Tmux and external cleanup paths run independently -----------------


async def test_t17_external_cleanup_works_without_any_tmux_infrastructure(tmp_path: Path) -> None:
    """T17: `_cleanup()` runs cleanly with no tmux runner, no SessionRegistry,
    and no on-disk tmux state. The external cleanup path is fully decoupled
    from the tmux liveness check.

    Calling `_cleanup()` directly bypasses the periodic loop and proves the
    service operates without any tmux-side wiring.

    **Validates: Requirements 4.1**
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    # Empty store — no bindings to consider. This must not raise.
    hook_server = _make_hook_server(has_pending=False)
    auto_approve = _make_auto_approve_service()
    reaper = ExternalBindingReaper(
        binding_store=store,
        auto_approve_service=auto_approve,
        hook_socket_server=hook_server,
    )
    service = ExternalBindingCleanupService(
        binding_store=store,
        hook_socket_server=hook_server,
        reaper=reaper,
        liveness_enabled=False,
        ttl=TTL,
        interval_sec=LONG_INTERVAL_SEC,
    )

    # No tmux runner exists, no SessionRegistryService exists. The cleanup
    # pass must succeed regardless.
    await service._cleanup()
    assert store.list_all() == []

    # Now add a stale binding and run a second pass to confirm the path also
    # works for non-empty stores in the absence of tmux infrastructure.
    stale_id = "stale-no-tmux"
    store.save_binding(
        ExternalBinding(
            session_id=stale_id,
            user_id=99,
            cwd="/home/user/no-tmux",
            bound_at=utc_now() - timedelta(hours=48),
            jsonl_path=None,
        )
    )

    await service._cleanup()
    assert store.get_binding(stale_id) is None, "external cleanup must remove stale bindings without any tmux dependency"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
