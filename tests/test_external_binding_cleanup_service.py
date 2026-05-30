"""Unit tests for ExternalBindingCleanupService.

Spec: stale-external-binding-cleanup (bugfix)

Covers test plan items T8-T13d from the bugfix:

- T8: Stale binding (idle_age > TTL) + no pending permission is removed by
  ``_cleanup()`` and the log entry uses ``reason="idle_ttl_expired"``.
- T9: Stale binding + ``has_pending_permission`` returns True is NOT removed
  (protection signal active).
- T10: Fresh binding (idle_age <= TTL) is NOT removed regardless of pending
  state.
- T11: After removal, ``auto_approve_service.clear_session(session_id)`` is
  awaited.
- T12: After removal, ``hook_socket_server.cancel_pending_permissions(
  session_id=session_id)`` is awaited.
- T12b: Stale binding + no HookSocketServer pending permission is still
  removed (cleanup service has no callback registry dependency).
- T12c: ``ExternalBindingCleanupService.__init__`` does NOT accept a
  ``permission_callback_registry`` parameter.
- T13: Removal log entry contains all required fields.
- T13c: Race - touch between snapshot and removal -> NOT removed.
- T13d: Binding removed between snapshot and removal -> cleanup skips
  gracefully without error and does NOT clear auto-approve for that session.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 4.6**
"""

from __future__ import annotations

import inspect
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

CLEANUP_LOGGER_NAME = "app.services.external_binding_reaper"
TTL = timedelta(hours=24)


# --- Helpers ----------------------------------------------------------------


def make_service(
    store: ExternalBindingStore,
    *,
    has_pending: bool = False,
    on_pending_check=None,
) -> tuple[ExternalBindingCleanupService, AsyncMock, AsyncMock]:
    """Construct the cleanup service with fully mocked async collaborators.

    ``has_pending``           - constant return value for
                                ``has_pending_permission`` when no
                                ``on_pending_check`` is supplied.
    ``on_pending_check``      - optional ``side_effect`` for
                                ``has_pending_permission`` (used to model
                                race-condition mutations of the store between
                                snapshot and removal).
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
        interval_sec=30.0,
    )
    return service, auto_approve, hook_server


def _save_binding(
    store: ExternalBindingStore,
    *,
    session_id: str,
    user_id: int = 42,
    cwd: str = "/home/user/project",
    age: timedelta,
    jsonl_path: str | None = None,
) -> ExternalBinding:
    """Save a binding whose ``bound_at`` (and therefore default
    ``last_activity_at``) is offset by ``age`` from now.
    """
    bound_at = utc_now() - age
    binding = ExternalBinding(
        session_id=session_id,
        user_id=user_id,
        cwd=cwd,
        bound_at=bound_at,
        jsonl_path=jsonl_path,
    )
    store.save_binding(binding)
    return binding


# --- T8: stale + no pending -> removed, reason idle_ttl_expired -------------


async def test_stale_binding_no_pending_is_removed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """**Validates: Requirements 2.1, 2.2, 2.3** (T8)"""
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="stale-t8", age=timedelta(hours=25))

    service, _auto_approve, _hook_server = make_service(store, has_pending=False)

    with caplog.at_level(logging.INFO, logger=CLEANUP_LOGGER_NAME):
        await service._cleanup()

    assert store.get_binding("stale-t8") is None, "stale binding should be removed"

    matching = [r for r in caplog.records if getattr(r, "session_id", None) == "stale-t8"]
    assert matching, "expected a removal log record for stale-t8"
    assert getattr(matching[0], "reason", None) == "idle_ttl_expired"


# --- T9: stale + pending -> protected ---------------------------------------


async def test_stale_binding_with_pending_permission_is_protected(tmp_path: Path) -> None:
    """**Validates: Requirements 2.5** (T9)"""
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="stale-t9", age=timedelta(hours=25))

    service, auto_approve, hook_server = make_service(store, has_pending=True)

    await service._cleanup()

    assert store.get_binding("stale-t9") is not None, "binding with pending permission must NOT be removed"
    auto_approve.clear_session.assert_not_awaited()
    hook_server.cancel_pending_permissions.assert_not_awaited()


# --- T10: fresh -> never removed --------------------------------------------


async def test_fresh_binding_is_never_removed(tmp_path: Path) -> None:
    """**Validates: Requirements 2.4** (T10)

    Fresh binding (idle_age <= TTL) is preserved regardless of whether a
    pending permission exists.
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="fresh-no-pending", age=timedelta(hours=1))
    _save_binding(store, session_id="fresh-with-pending", age=timedelta(minutes=5))

    # Even with the protection signal active, the fresh binding should not
    # reach the protection check because it fails the snapshot pre-filter.
    service, auto_approve, hook_server = make_service(store, has_pending=True)
    await service._cleanup()

    assert store.get_binding("fresh-no-pending") is not None
    assert store.get_binding("fresh-with-pending") is not None
    auto_approve.clear_session.assert_not_awaited()
    hook_server.cancel_pending_permissions.assert_not_awaited()


# --- T11 / T12: side-effect cleanups are awaited ----------------------------


async def test_removal_awaits_auto_approve_clear_session(tmp_path: Path) -> None:
    """**Validates: Requirements 2.6** (T11)"""
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="stale-t11", age=timedelta(hours=30))

    service, auto_approve, _hook_server = make_service(store, has_pending=False)

    await service._cleanup()

    auto_approve.clear_session.assert_awaited_once_with("stale-t11")


async def test_removal_awaits_cancel_pending_permissions(tmp_path: Path) -> None:
    """**Validates: Requirements 2.6** (T12)"""
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="stale-t12", age=timedelta(hours=30))

    service, _auto_approve, hook_server = make_service(store, has_pending=False)

    await service._cleanup()

    hook_server.cancel_pending_permissions.assert_awaited_once_with(session_id="stale-t12")


# --- T12b: cleanup service has no callback-registry dependency --------------


async def test_stale_binding_removed_without_callback_registry_dependency(tmp_path: Path) -> None:
    """**Validates: Requirements 2.6** (T12b)

    The cleanup service has no permission-callback-registry parameter, so
    "callback registry records exist" is not even an input - the binding is
    still removed when the HookSocketServer reports no pending permission.
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="stale-t12b", age=timedelta(hours=30))

    service, _auto_approve, _hook_server = make_service(store, has_pending=False)

    await service._cleanup()

    assert store.get_binding("stale-t12b") is None


# --- T12c: constructor signature pinning ------------------------------------


def test_constructor_does_not_accept_permission_callback_registry() -> None:
    """**Validates: Requirements 4.1, 4.2** (T12c)

    The cleanup service intentionally has no PermissionCallbackRegistry
    dependency. Pinning the constructor parameter set guards that intent.
    """
    params = set(inspect.signature(ExternalBindingCleanupService.__init__).parameters)

    assert params == {
        "self",
        "binding_store",
        "hook_socket_server",
        "reaper",
        "liveness_enabled",
        "ttl",
        "interval_sec",
    }
    assert "permission_callback_registry" not in params


# --- T13: removal log contains full context ---------------------------------


async def test_removal_log_contains_full_context(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """**Validates: Requirements 2.3** (T13)"""
    store = ExternalBindingStore(data_dir=tmp_path)
    binding = _save_binding(
        store,
        session_id="stale-t13",
        user_id=4242,
        cwd="/home/jack/work",
        age=timedelta(hours=27),
    )

    service, _auto_approve, _hook_server = make_service(store, has_pending=False)

    with caplog.at_level(logging.INFO, logger=CLEANUP_LOGGER_NAME):
        await service._cleanup()

    matching = [r for r in caplog.records if getattr(r, "session_id", None) == "stale-t13"]
    assert matching, "expected a removal log record for stale-t13"
    record = matching[0]

    extras = record.__dict__
    assert extras["session_id"] == "stale-t13"
    assert extras["user_id"] == 4242
    assert extras["cwd"] == "/home/jack/work"
    assert extras["bound_at"] == binding.bound_at.isoformat()
    assert extras["last_activity_at"] == binding.last_activity_at.isoformat()
    assert extras["reason"] == "idle_ttl_expired"

    idle_hours = extras["idle_hours"]
    assert isinstance(idle_hours, float)
    # Idle age >= 27h since we constructed bound_at 27 hours in the past.
    assert idle_hours >= 27.0


# --- T13c: race - touch_activity refreshes timestamp before removal ---------


async def test_race_touch_activity_between_snapshot_and_removal(tmp_path: Path) -> None:
    """**Validates: Requirements 4.6** (T13c)

    The cleanup service's step-v re-read after the protection-signal await
    must observe a fresh ``last_activity_at`` if a concurrent
    ``touch_activity`` raced in, and SHALL NOT remove the binding.
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="stale-t13c", age=timedelta(hours=30))

    def refresh_then_say_no_pending(*, session_id: str) -> bool:
        # Simulate a hook event landing during the await: the activity
        # timestamp is refreshed to "now" so idle_age becomes ~0.
        store.touch_activity(session_id, utc_now())
        return False

    service, auto_approve, hook_server = make_service(
        store,
        on_pending_check=refresh_then_say_no_pending,
    )

    await service._cleanup()

    assert store.get_binding("stale-t13c") is not None, "binding refreshed during cleanup must NOT be removed by the final re-read"
    auto_approve.clear_session.assert_not_awaited()
    hook_server.cancel_pending_permissions.assert_not_awaited()


# --- T13d: binding removed mid-cleanup -> graceful skip ---------------------


async def test_race_binding_removed_between_snapshot_and_removal(tmp_path: Path) -> None:
    """**Validates: Requirements 4.6, 3.5** (T13d)

    If the binding is removed by another path (e.g. SessionEnd handler) while
    the cleanup service is awaiting the protection check, the cleanup must
    skip without error and must NOT clear auto-approve for that session.
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    _save_binding(store, session_id="stale-t13d", age=timedelta(hours=30))

    def remove_then_say_no_pending(*, session_id: str) -> bool:
        # Simulate SessionEnd arriving during the await: binding vanishes.
        store.remove_binding(session_id)
        return False

    service, auto_approve, hook_server = make_service(
        store,
        on_pending_check=remove_then_say_no_pending,
    )

    # Must not raise.
    await service._cleanup()

    assert store.get_binding("stale-t13d") is None, "binding was removed by the racing path"
    # The cleanup service must NOT redundantly clean side-channel state for a
    # session it did not itself remove.
    auto_approve.clear_session.assert_not_awaited()
    hook_server.cancel_pending_permissions.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
