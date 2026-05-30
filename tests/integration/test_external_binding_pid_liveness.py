"""End-to-end integration tests for external-binding pid liveness (task 12.3).

These tests wire the REAL collaborators directly (no full bootstrap):

  - a real ``ExternalBindingStore`` over a tmp dir,
  - a real ``ExternalBindingReaper`` over that store with mocked
    ``auto_approve_service`` / ``hook_socket_server``,
  - a real ``ExternalBindingCleanupService`` driving the reaper, and
  - the real ``/list`` aiogram handler (invoked via the registered router
    callback).

MOCK STRATEGY (per tasks.md task 12.3): liveness is driven by patching the
consumer-local ``process_is_alive`` seam, NOT ``os.kill``. The cleanup loop
imports ``process_is_alive`` into
``app.services.external_binding_cleanup_service`` and ``/list`` imports it into
``app.bot.handlers.command_list``; each test patches the relevant module-local
name and only steers the alive/dead verdict via ``return_value`` /
``side_effect``. The probe's own mapping is trusted (pinned by Properties 1-2).

Requirements: 5.1, 5.2, 6.1, 6.2, 6.3, 6.5, 7.2, 9.1, 9.2, 10.2, 10.3, 11.1, 11.4
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Router

from app.bot.handlers.command_list import register_list_handler
from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_cleanup_service import ExternalBindingCleanupService
from app.services.external_binding_reaper import ExternalBindingReaper
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService

CLEANUP_PROBE = "app.services.external_binding_cleanup_service.process_is_alive"
LIST_PROBE = "app.bot.handlers.command_list.process_is_alive"

_TTL = timedelta(hours=24)
_USER_ID = 42


# --------------------------------------------------------------------------
# Wiring helpers (REAL store + reaper + cleanup service, mocked async deps)
# --------------------------------------------------------------------------


def _wire(tmp_path: Path, *, liveness_enabled: bool, has_pending: bool = False) -> SimpleNamespace:
    """Wire the real cleanup service + reaper + store with mocked async deps.

    ``auto_approve_service`` and ``hook_socket_server`` are explicit AsyncMocks
    so their coroutine methods are awaitable and assertable. The reaper and the
    cleanup service share the SAME ``hook_socket_server`` mock (the cleanup loop
    awaits ``has_pending_permission`` on it; the reaper awaits
    ``cancel_pending_permissions`` on it), exactly as production wiring does.
    """
    store = ExternalBindingStore(data_dir=tmp_path / "data")

    auto_approve = AsyncMock()
    auto_approve.clear_session = AsyncMock()

    hook_socket = AsyncMock()
    hook_socket.has_pending_permission = AsyncMock(return_value=has_pending)
    hook_socket.cancel_pending_permissions = AsyncMock()

    reaper = ExternalBindingReaper(
        binding_store=store,
        auto_approve_service=auto_approve,
        hook_socket_server=hook_socket,
    )
    service = ExternalBindingCleanupService(
        binding_store=store,
        hook_socket_server=hook_socket,
        reaper=reaper,
        liveness_enabled=liveness_enabled,
        ttl=_TTL,
        interval_sec=30.0,
    )
    return SimpleNamespace(
        store=store,
        auto_approve=auto_approve,
        hook_socket=hook_socket,
        reaper=reaper,
        service=service,
    )


def _save_binding(
    store: ExternalBindingStore,
    *,
    session_id: str,
    pid: int | None,
    idle_hours: float,
    user_id: int = _USER_ID,
    cwd: str = "/home/user/project",
) -> ExternalBinding:
    """Persist a binding whose ``last_activity_at`` is ``idle_hours`` in the past."""
    when = utc_now() - timedelta(hours=idle_hours)
    binding = ExternalBinding(
        session_id=session_id,
        user_id=user_id,
        cwd=cwd,
        bound_at=when,
        jsonl_path=f"/tmp/projects/{session_id}.jsonl",
        pid=pid,
        last_activity_at_init=when,
    )
    store.save_binding(binding)
    return binding


def _build_list_handler(
    *,
    store: ExternalBindingStore,
    reaper: object | None,
    liveness_enabled: bool,
):
    """Register the real /list handler and return its inner coroutine.

    aiogram 3.28: a router's message handlers are ``HandlerObject`` instances
    whose ``.callback`` is the registered coroutine (the pattern used by
    ``tests/test_session_handlers.py``).
    """
    router = Router()
    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    binder = ExternalSessionBinder(
        discovery=ExternalSessionDiscoveryService(),
        binding_store=store,
        projects_dir=Path("/tmp/projects"),
    )
    register_list_handler(
        router,
        registry_service=registry,
        external_binder=binder,
        liveness_enabled=liveness_enabled,
        reaper=reaper,
    )
    return router.message.handlers[-1].callback


def _make_message(user_id: int = _USER_ID) -> MagicMock:
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.answer = AsyncMock()
    return message


def _answer_text(message: MagicMock) -> str:
    """Return the rendered text passed to the first ``message.answer`` call."""
    return message.answer.call_args.args[0]


def _answer_callback_data(message: MagicMock) -> list[str]:
    """Return the inline-keyboard callback_data of the first ``answer`` call.

    Bound sessions render as inline-keyboard buttons (the message text carries
    only the section header), so visibility of a specific binding is asserted
    against the keyboard, not the text body.
    """
    keyboard = message.answer.call_args.kwargs.get("reply_markup")
    if keyboard is None:
        return []
    return [btn.callback_data for row in keyboard.inline_keyboard for btn in row]


def _removal_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "external binding removed"]


# --------------------------------------------------------------------------
# Scenario 1 — dead-process removal in a single _cleanup() pass
# --------------------------------------------------------------------------


async def test_dead_process_removed_in_one_cleanup_pass(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Liveness enabled + known pid + probe dead -> removed with full cleanup.

    Validates: Requirements 6.1, 6.3, 6.5
    """
    caplog.set_level(logging.INFO, logger="app.services.external_binding_reaper")
    ctx = _wire(tmp_path, liveness_enabled=True)
    session_id = "sess-dead-01"
    # Fresh binding (idle_age <= TTL) to prove the dead pid overrides idle age.
    _save_binding(ctx.store, session_id=session_id, pid=4242, idle_hours=0)

    with patch(CLEANUP_PROBE, return_value=False):
        await ctx.service._cleanup()

    # Binding dropped from the store.
    assert ctx.store.get_binding(session_id) is None
    # Associated state unwound via the shared reaper.
    ctx.auto_approve.clear_session.assert_awaited_once_with(session_id)
    ctx.hook_socket.cancel_pending_permissions.assert_awaited_once_with(session_id=session_id)
    # Reason label is pid_dead.
    records = _removal_records(caplog)
    assert len(records) == 1
    assert records[0].reason == "pid_dead"
    assert records[0].session_id == session_id


# --------------------------------------------------------------------------
# Scenario 2 — live process retained even when idle age exceeds TTL
# --------------------------------------------------------------------------


async def test_live_idle_binding_retained(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Liveness enabled + known pid + probe alive + idle_age > TTL -> KEEP.

    Validates: Requirements 5.1, 5.2
    """
    caplog.set_level(logging.INFO, logger="app.services.external_binding_reaper")
    ctx = _wire(tmp_path, liveness_enabled=True)
    session_id = "sess-live-02"
    _save_binding(ctx.store, session_id=session_id, pid=1234, idle_hours=48)

    with patch(CLEANUP_PROBE, return_value=True):
        await ctx.service._cleanup()

    # Live pid overrides idle TTL: binding retained, nothing cleaned, no log.
    assert ctx.store.get_binding(session_id) is not None
    ctx.auto_approve.clear_session.assert_not_awaited()
    ctx.hook_socket.cancel_pending_permissions.assert_not_awaited()
    assert _removal_records(caplog) == []


# --------------------------------------------------------------------------
# Scenario 3 — unknown pid falls back to idle-TTL path
# --------------------------------------------------------------------------


async def test_unknown_pid_idle_ttl_fallback(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Liveness enabled + pid=None + idle_age > TTL + no pending -> idle removal.

    Validates: Requirements 7.2, 11.1
    """
    caplog.set_level(logging.INFO, logger="app.services.external_binding_reaper")
    ctx = _wire(tmp_path, liveness_enabled=True, has_pending=False)
    session_id = "sess-unknown-03"
    _save_binding(ctx.store, session_id=session_id, pid=None, idle_hours=48)

    # Probe should never be consulted on the idle path (pid unknown); patch it
    # dead to prove the removal is driven by idle TTL, not liveness.
    with patch(CLEANUP_PROBE, return_value=False) as probe:
        await ctx.service._cleanup()

    probe.assert_not_called()
    assert ctx.store.get_binding(session_id) is None
    ctx.auto_approve.clear_session.assert_awaited_once_with(session_id)
    ctx.hook_socket.cancel_pending_permissions.assert_awaited_once_with(session_id=session_id)
    records = _removal_records(caplog)
    assert len(records) == 1
    assert records[0].reason == "idle_ttl_expired"
    # Req 8.3: pid rendered as explicit None on the unknown-pid path.
    assert records[0].pid is None


# --------------------------------------------------------------------------
# Scenario 4 — /list hides and reaps a dead binding between cleanup cycles
# --------------------------------------------------------------------------


async def test_list_hides_and_reaps_dead_binding(tmp_path: Path) -> None:
    """/list with liveness enabled excludes a dead binding and reaps it.

    Validates: Requirements 9.1, 9.2
    """
    ctx = _wire(tmp_path, liveness_enabled=True)
    session_id = "sess-listdead-04"
    _save_binding(ctx.store, session_id=session_id, pid=9999, idle_hours=0)

    handler = _build_list_handler(store=ctx.store, reaper=ctx.reaper, liveness_enabled=True)
    message = _make_message()

    with patch(LIST_PROBE, return_value=False):
        await handler(message)

    # The dead binding is not rendered (it was the only session -> empty list).
    text = _answer_text(message)
    assert session_id[:8] not in text
    # And it was reaped through the shared reaper, so it does not reappear.
    assert ctx.store.get_binding(session_id) is None
    ctx.auto_approve.clear_session.assert_awaited_once_with(session_id)
    ctx.hook_socket.cancel_pending_permissions.assert_awaited_once_with(session_id=session_id)


# --------------------------------------------------------------------------
# Scenario 5 — liveness disabled: idle path keeps fresh binding; /list no-exclude
# --------------------------------------------------------------------------


async def test_liveness_disabled_retains_and_lists_dead_pid(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Liveness disabled -> pid ignored everywhere (idle-TTL path + /list).

    Cleanup: a dead pid with idle_age <= TTL is RETAINED (fresh-binding KEEP).
    /list: performs NO pid-based exclusion and never invokes the reaper.

    Validates: Requirements 10.2, 10.3, 11.1
    """
    caplog.set_level(logging.INFO, logger="app.services.external_binding_reaper")
    ctx = _wire(tmp_path, liveness_enabled=False)
    session_id = "sess-disabled-05"
    _save_binding(ctx.store, session_id=session_id, pid=4242, idle_hours=0)

    # --- cleanup part: liveness disabled => idle path; fresh => KEEP ---
    with patch(CLEANUP_PROBE, return_value=False) as cleanup_probe:
        await ctx.service._cleanup()

    cleanup_probe.assert_not_called()  # liveness disabled, probe never consulted
    assert ctx.store.get_binding(session_id) is not None
    ctx.auto_approve.clear_session.assert_not_awaited()
    ctx.hook_socket.cancel_pending_permissions.assert_not_awaited()
    assert _removal_records(caplog) == []

    # --- /list part: liveness disabled => no pid exclusion, no reap ---
    list_reaper = AsyncMock()
    list_reaper.remove_with_cleanup = AsyncMock()
    handler = _build_list_handler(store=ctx.store, reaper=list_reaper, liveness_enabled=False)
    message = _make_message()

    with patch(LIST_PROBE, return_value=False) as list_probe:
        await handler(message)

    list_probe.assert_not_called()
    list_reaper.remove_with_cleanup.assert_not_awaited()
    # Binding still present and rendered as a bound-session button.
    assert ctx.store.get_binding(session_id) is not None
    assert f"sess:select:{session_id[:16]}" in _answer_callback_data(message)


# --------------------------------------------------------------------------
# Scenario 6 — migration: pre-feature JSON (no pid keys) loads with pid=None
# --------------------------------------------------------------------------


def test_migration_pre_feature_json_loads_pid_none(tmp_path: Path) -> None:
    """A pre-feature external_bindings.json (no pid keys) loads with pid=None.

    Validates: Requirements 11.4
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "sess-legacy-a": {
            "user_id": 10,
            "cwd": "/home/alice/proj",
            "bound_at": "2026-01-01T00:00:00+00:00",
            "last_activity_at": "2026-01-01T00:05:00+00:00",
            "jsonl_path": "/tmp/projects/-home-alice-proj/sess-legacy-a.jsonl",
        },
        "sess-legacy-b": {
            "user_id": 20,
            "cwd": "/home/bob/work",
            "bound_at": "2026-01-02T00:00:00+00:00",
            "last_activity_at": "2026-01-02T01:00:00+00:00",
            "jsonl_path": None,
        },
    }
    (data_dir / "external_bindings.json").write_text(json.dumps(legacy, indent=2), encoding="utf-8")

    store = ExternalBindingStore(data_dir=data_dir)
    loaded = store.load_all()

    assert set(loaded) == {"sess-legacy-a", "sess-legacy-b"}
    assert all(b.pid is None for b in loaded.values())
    # Other fields still load intact (no silent coercion).
    assert loaded["sess-legacy-a"].user_id == 10
    assert loaded["sess-legacy-b"].cwd == "/home/bob/work"
