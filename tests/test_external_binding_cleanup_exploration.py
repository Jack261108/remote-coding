"""Bug condition exploration test for stale external binding cleanup.

Spec: stale-external-binding-cleanup (bugfix)
Property 1: Bug Condition - Stale External Binding Never Cleaned Up

This test is written TDD-style: it asserts the EXPECTED (correct) behavior —
after the cleanup service runs, a stale binding should NOT be present in the
store. On UNFIXED code this test FAILS because `ExternalBindingCleanupService`
does not exist yet (ImportError at import time). That failure CONFIRMS the bug:
there is no mechanism that removes stale external bindings.

After the fix is implemented (subsequent tasks), the SAME test should pass.

**Validates: Requirements 1.1, 1.2, 1.4, 2.1, 2.2, 2.3**
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now

# This import is expected to raise ImportError on UNFIXED code, which is the
# success signal for this exploration test (it confirms the bug exists because
# no cleanup component is present in the codebase).
from app.services.external_binding_cleanup_service import ExternalBindingCleanupService
from app.services.external_binding_reaper import ExternalBindingReaper
from app.services.external_binding_store import ExternalBindingStore

TTL = timedelta(hours=24)


def _make_hook_server(*, has_pending: bool = False) -> AsyncMock:
    """Build a mock HookSocketServer exposing the async protection-signal API."""
    hook_server = AsyncMock()
    hook_server.has_pending_permission = AsyncMock(return_value=has_pending)
    hook_server.cancel_pending_permissions = AsyncMock(return_value=None)
    return hook_server


def _make_auto_approve_service() -> AsyncMock:
    auto_approve = AsyncMock()
    auto_approve.clear_session = AsyncMock(return_value=None)
    return auto_approve


def _make_cleanup_service(
    store: ExternalBindingStore,
    hook_server: AsyncMock,
    auto_approve: AsyncMock,
) -> ExternalBindingCleanupService:
    reaper = ExternalBindingReaper(
        binding_store=store,
        auto_approve_service=auto_approve,
        hook_socket_server=hook_server,
    )
    return ExternalBindingCleanupService(
        binding_store=store,
        hook_socket_server=hook_server,
        reaper=reaper,
        liveness_enabled=False,
        ttl=TTL,
        interval_sec=30.0,
    )


async def test_cleanup_removes_stale_binding(tmp_path: Path) -> None:
    """A binding older than the idle TTL with no pending permission is removed
    by the cleanup service.

    On UNFIXED code: ImportError (service does not exist) -> test FAILS.
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    session_id = "stale-session-abc123"
    store.save_binding(
        ExternalBinding(
            session_id=session_id,
            user_id=42,
            cwd="/home/user/project",
            bound_at=utc_now() - timedelta(hours=25),
            jsonl_path=None,
        )
    )

    hook_server = _make_hook_server(has_pending=False)
    auto_approve = _make_auto_approve_service()
    service = _make_cleanup_service(store, hook_server, auto_approve)

    await service._cleanup()

    assert store.get_binding(session_id) is None, "stale binding should have been removed by cleanup"


async def test_startup_cleanup_removes_stale_binding_after_restart(tmp_path: Path) -> None:
    """A 48h-old binding loaded from JSON at startup is removed by the initial
    cleanup run triggered via `start()`.

    On UNFIXED code: ImportError (service does not exist) -> test FAILS.
    """
    session_id = "stale-session-def456"
    stale_bound_at = utc_now() - timedelta(hours=48)
    payload = {
        session_id: {
            "user_id": 7,
            "cwd": "/home/user/other",
            "bound_at": stale_bound_at.isoformat(),
            "jsonl_path": None,
        }
    }
    (tmp_path / "external_bindings.json").write_text(json.dumps(payload), encoding="utf-8")

    # New store instance loads the persisted (stale) binding, simulating restart.
    store = ExternalBindingStore(data_dir=tmp_path)
    assert store.get_binding(session_id) is not None, "precondition: stale binding loaded from JSON"

    hook_server = _make_hook_server(has_pending=False)
    auto_approve = _make_auto_approve_service()
    service = _make_cleanup_service(store, hook_server, auto_approve)

    await service.start()
    try:
        assert store.get_binding(session_id) is None, "stale binding should be gone after startup cleanup"
    finally:
        await service.stop()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
