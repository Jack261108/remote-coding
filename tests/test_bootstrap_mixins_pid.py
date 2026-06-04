"""SessionEnd preservation in the hook-dispatcher ownership-resolution path.

These tests pin the behavior of ``_resolve_ownership_stage`` in
``app/bootstrap_mixins.py`` with respect to the pid-liveness feature:

* A ``SessionEnd`` event removes the binding via the EXISTING handler and runs
  its existing associated-state cleanup set
  (``deactivate_all_for_session``, ``release_all_slots_for_session``,
  ``permission_callback_registry.invalidate_session``,
  ``unbound_permission_handler.invalidate_session``, ``remove_binding``), does
  NOT refresh activity via ``touch_activity`` (so the stored ``pid`` is never
  updated), and does NOT invoke the cleanup reaper. The SessionEnd path is
  independent of the dead-process reaper.
* As the contrasting delta: a bound, non-``SessionEnd`` external event DOES
  refresh activity through ``touch_activity(..., pid=event.pid)``.

The harness mirrors the existing
``test_session_end_runs_unified_permission_cleanup_in_order`` pattern in
``tests/test_bootstrap_hooks.py`` (an ``AppContainer`` subclass with a custom
``__init__`` wiring lightweight mock collaborators).

Validates: Requirements 4.4, 11.2
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bootstrap import AppContainer
from app.domain.external_session_models import ExternalBinding, OwnershipResult, SessionOrigin
from app.domain.hook_models import HookEvent
from app.domain.models import utc_now
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.session_ownership_resolver import SessionOwnershipResolver


class _RecordingBindingStore:
    """Minimal ``external_binding_store`` stand-in.

    Records ``remove_binding`` and ``touch_activity`` calls so a test can assert
    that the SessionEnd path removes the binding but never refreshes
    activity/pid. ``get_binding`` returns a non-None sentinel so the bound
    non-SessionEnd path's "binding exists" guard is satisfied.
    """

    def __init__(self) -> None:
        self.removed: list[str] = []
        self.touch_calls: list[tuple[str, object, int | None]] = []
        self._binding = object()  # sentinel non-None binding

    def remove_binding(self, session_id: str) -> None:
        self.removed.append(session_id)

    def get_binding(self, session_id: str) -> object | None:
        return self._binding

    def touch_activity(
        self,
        session_id: str,
        last_activity_at: object,
        *,
        persist_min_interval_sec: int = 60,
        pid: int | None = None,
    ) -> None:
        self.touch_calls.append((session_id, last_activity_at, pid))


class _EmptySessionService:
    async def list_all(self) -> list:
        return []


class _RecordingDiscovery(ExternalSessionDiscoveryService):
    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[str] = []
        self.removed: list[str] = []

    def record_event(self, event: HookEvent) -> None:
        self.recorded.append(event.session_id)
        super().record_event(event)

    def remove_session(self, session_id: str) -> None:
        self.removed.append(session_id)
        super().remove_session(session_id)


def _make_lifecycle_container(tmp_path, binding_store, discovery, seen):
    class _Container(AppContainer):
        def __init__(self) -> None:
            self.settings = SimpleNamespace(allowed_workdirs=[str(tmp_path)])
            self.external_binding_store = binding_store
            self.external_discovery = discovery
            self.ownership_resolver = SessionOwnershipResolver(
                session_service=_EmptySessionService(),
                binding_store=binding_store,
            )

        async def _dispatch_session_event(self, event) -> None:  # type: ignore[override]
            seen.append(f"dispatch:{event.session_id}")

        def _schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
            seen.append(f"sync:{session_id}")

        async def _run_auto_approve_check(self, *args, **kwargs):
            return None

        def _maybe_auto_file_send(self, event: HookEvent, owner_user_id: int | None) -> None:
            return None

    return _Container()


def _make_session_end_container(tmp_path, store, reaper, seen):
    """Build an AppContainer subclass wired for the legacy SessionEnd path.

    Mirrors the existing harness in test_bootstrap_hooks.py: collaborators are
    lightweight stand-ins that record their calls into ``seen``. No
    ``ownership_resolver`` is wired, so after the SessionEnd cleanup the method
    falls through to the legacy bind/dispatch/sync fallback (overridden here).
    """

    class _AutoApprove:
        async def deactivate_all_for_session(self, session_id: str) -> int:
            seen.append(f"aas_deactivate:{session_id}")
            return 1

        async def release_all_slots_for_session(self, session_id: str) -> int:
            seen.append(f"aas_release:{session_id}")
            return 1

    class _Registry:
        async def invalidate_session(self, session_id: str) -> int:
            seen.append(f"registry:{session_id}")
            return 1

    class _Unbound:
        async def invalidate_session(self, session_id: str) -> int:
            seen.append(f"unbound:{session_id}")
            return 1

    class _Container(AppContainer):
        def __init__(self) -> None:
            self.settings = SimpleNamespace(allowed_workdirs=[str(tmp_path)])
            self.auto_approve_service = _AutoApprove()
            self.permission_callback_registry = _Registry()
            self.unbound_permission_handler = _Unbound()
            self.external_binding_store = store
            # Attached so the test can prove the SessionEnd path never touches it.
            self.external_binding_reaper = reaper

        async def _bind_hook_session(self, event: HookEvent) -> None:
            seen.append(f"bind:{event.session_id}")

        async def _dispatch_session_event(self, event) -> None:  # type: ignore[override]
            seen.append(f"dispatch:{event.session_id}")

        def _schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
            seen.append(f"sync:{session_id}")

    return _Container()


@pytest.mark.asyncio
async def test_bound_external_session_end_removes_binding_without_rediscovery(tmp_path) -> None:
    seen: list[str] = []
    binding_store = ExternalBindingStore(data_dir=tmp_path)
    session_id = "ended-external-bound"
    binding_store.save_binding(
        ExternalBinding(
            session_id=session_id,
            user_id=1,
            cwd=str(tmp_path),
            bound_at=utc_now(),
            jsonl_path=None,
            pid=None,
        )
    )
    discovery = _RecordingDiscovery()
    container = _make_lifecycle_container(tmp_path, binding_store, discovery, seen)

    await container._handle_hook_event(
        HookEvent(
            session_id=session_id,
            cwd=str(tmp_path),
            event="SessionEnd",
            status="ended",
        )
    )

    assert binding_store.get_binding(session_id) is None
    assert discovery.get(session_id) is None
    assert discovery.recorded == []
    assert seen == [f"dispatch:{session_id}", f"sync:{session_id}"]


@pytest.mark.asyncio
async def test_bound_external_ended_status_removes_binding_without_rediscovery(tmp_path) -> None:
    seen: list[str] = []
    binding_store = ExternalBindingStore(data_dir=tmp_path)
    session_id = "ended-status-bound"
    binding_store.save_binding(
        ExternalBinding(
            session_id=session_id,
            user_id=1,
            cwd=str(tmp_path),
            bound_at=utc_now(),
            jsonl_path=None,
            pid=4242,
        )
    )
    discovery = _RecordingDiscovery()
    container = _make_lifecycle_container(tmp_path, binding_store, discovery, seen)

    await container._handle_hook_event(
        HookEvent(
            session_id=session_id,
            cwd=str(tmp_path),
            event="Stop",
            status="ended",
            pid=4242,
        )
    )

    assert binding_store.get_binding(session_id) is None
    assert discovery.get(session_id) is None
    assert discovery.recorded == []
    assert seen == [f"dispatch:{session_id}", f"sync:{session_id}"]


@pytest.mark.asyncio
async def test_existing_unbound_session_end_removes_discovery_session(tmp_path) -> None:
    seen: list[str] = []
    binding_store = ExternalBindingStore(data_dir=tmp_path)
    discovery = _RecordingDiscovery()
    session_id = "ended-existing-unbound"
    discovery.record_event(
        HookEvent(
            session_id=session_id,
            cwd=str(tmp_path),
            event="PreToolUse",
            status="running",
        )
    )
    container = _make_lifecycle_container(tmp_path, binding_store, discovery, seen)

    await container._handle_hook_event(
        HookEvent(
            session_id=session_id,
            cwd=str(tmp_path),
            event="SessionEnd",
            status="ended",
        )
    )

    assert discovery.get(session_id) is None
    assert discovery.removed == [session_id]
    assert discovery.recorded == [session_id]
    assert seen == []


@pytest.mark.asyncio
async def test_unknown_unbound_session_end_does_not_create_discovery_session(tmp_path) -> None:
    seen: list[str] = []
    binding_store = ExternalBindingStore(data_dir=tmp_path)
    discovery = _RecordingDiscovery()
    session_id = "ended-unknown-unbound"
    container = _make_lifecycle_container(tmp_path, binding_store, discovery, seen)

    await container._handle_hook_event(
        HookEvent(
            session_id=session_id,
            cwd=str(tmp_path),
            event="SessionEnd",
            status="ended",
        )
    )

    assert discovery.get(session_id) is None
    assert discovery.removed == [session_id]
    assert discovery.recorded == []
    assert seen == []


@pytest.mark.asyncio
async def test_session_end_removes_binding_without_touch_or_reaper(tmp_path) -> None:
    """SessionEnd removes the binding + runs the cleanup set, never touching pid.

    Validates: Requirements 4.4, 11.2
    """
    seen: list[str] = []
    store = _RecordingBindingStore()
    reaper = AsyncMock()

    container = _make_session_end_container(tmp_path, store, reaper, seen)
    event = HookEvent(
        session_id="ended-session",
        cwd=str(tmp_path),
        event="SessionEnd",
        status="ended",
        pid=4242,
    )

    await container._resolve_ownership_stage(event)

    # The existing SessionEnd cleanup set ran in its canonical order...
    assert seen[:4] == [
        "aas_deactivate:ended-session",
        "aas_release:ended-session",
        "registry:ended-session",
        "unbound:ended-session",
    ]
    # ...and the binding was removed via the existing handler.
    assert store.removed == ["ended-session"]

    # touch_activity is NOT called for SessionEnd, so pid is never updated (Req 4.4).
    assert store.touch_calls == []

    # The SessionEnd path is independent of the dead-process reaper (Req 11.2).
    reaper.remove_with_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_bound_non_session_end_event_refreshes_pid(tmp_path) -> None:
    """Contrast/delta: a bound non-SessionEnd event refreshes activity with pid.

    Confirms the SessionEnd skip in the prior test is specific to SessionEnd:
    an ordinary bound external event still calls
    ``touch_activity(session_id, now, pid=event.pid)``.

    Validates: Requirements 4.4, 11.2
    """
    store = _RecordingBindingStore()
    reaper = AsyncMock()

    class _Container(AppContainer):
        def __init__(self) -> None:
            self.settings = SimpleNamespace(allowed_workdirs=[str(tmp_path)])
            self.external_binding_store = store
            self.external_binding_reaper = reaper
            self.ownership_resolver = SimpleNamespace(
                resolve=AsyncMock(
                    return_value=OwnershipResult(
                        owner_user_id=1,
                        origin=SessionOrigin.EXTERNAL,
                        ownership_state="bound",
                    )
                )
            )

    container = _Container()
    event = HookEvent(
        session_id="bound-session",
        cwd=str(tmp_path),
        event="PostToolUse",
        status="running",
        pid=4242,
    )

    ownership = await container._resolve_ownership_stage(event)

    # Ownership resolved as a bound external session (guards against a silently
    # swallowed exception in _resolve_ownership_stage masking the assertions).
    assert ownership is not None
    assert ownership.ownership_state == "bound"

    # Activity refreshed exactly once, carrying the event's pid.
    assert len(store.touch_calls) == 1
    session_id, _, pid = store.touch_calls[0]
    assert session_id == "bound-session"
    assert pid == 4242

    # Still independent of the reaper.
    reaper.remove_with_cleanup.assert_not_awaited()
