"""Unit test for defensive bind-time pid capture in ``ExternalSessionBinder``.

Spec: external-binding-pid-liveness (task 5.3)

Covers Requirement 3.3: IF capturing the ``pid`` fails for any reason (internal
error or race), THEN the Session_Binder SHALL still create the Binding
successfully with ``pid`` set to None, prioritizing bind availability over pid
completeness.

This test forces access to ``unbound.pid`` to raise and asserts that:
- the bind still SUCCEEDS (``BindResult.success`` is True), and
- the stored binding has ``pid is None``.

**Validates: Requirements 3.3**
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from tests.fakes.process_probe import FakeLocalProcessProbe


class _RaisingPidUnbound:
    """Stand-in for ``UnboundExternalSession`` whose ``pid`` access raises.

    Exposes the attributes ``ExternalSessionBinder.bind`` reads from the unbound
    session (``session_id`` and ``cwd``) as plain attributes, while ``pid`` is a
    property that raises to simulate an internal error / race at capture time.
    """

    def __init__(self, *, session_id: str, cwd: str) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.first_seen = datetime.now(UTC)
        self.last_seen = datetime.now(UTC)
        self.event_count = 1
        self.title = None

    @property
    def pid(self) -> int:
        raise RuntimeError("boom")


class _DiscoveryDouble:
    """Minimal discovery double exposing only what ``bind`` calls.

    ``get`` returns the raising-pid unbound session; ``remove_session`` is a
    no-op (the real discovery would drop the session from tracking here).
    """

    def __init__(self, unbound: _RaisingPidUnbound) -> None:
        self._unbound = unbound
        self.removed: list[str] = []

    def get(self, session_id: str) -> _RaisingPidUnbound | None:
        if session_id == self._unbound.session_id:
            return self._unbound
        return None

    def remove_session(self, session_id: str) -> None:
        self.removed.append(session_id)


async def test_bind_succeeds_with_pid_none_when_pid_capture_raises(tmp_path: Path) -> None:
    """**Validates: Requirements 3.3**

    When ``unbound.pid`` access raises, ``bind`` degrades gracefully: it still
    succeeds and the stored binding carries ``pid is None``.
    """
    session_id = "sess-x"
    store = ExternalBindingStore(data_dir=tmp_path)
    unbound = _RaisingPidUnbound(session_id=session_id, cwd="/home/user/project")
    discovery = _DiscoveryDouble(unbound)

    binder = ExternalSessionBinder(
        discovery=discovery,  # type: ignore[arg-type]
        binding_store=store,
        projects_dir=Path("/tmp/projects"),
        sync_callback=None,
    )

    result = await binder.bind(user_id=1, session_id=session_id)

    # Bind must succeed even though pid capture failed.
    assert result.success is True

    # The stored binding must exist and carry pid=None (degraded capture).
    stored = store.get_binding(session_id)
    assert stored is not None
    assert stored.pid is None


async def test_bind_and_unbind_use_lifecycle_callbacks(tmp_path: Path) -> None:
    session_id = "sess-callback"
    store = ExternalBindingStore(data_dir=tmp_path)
    unbound = _RaisingPidUnbound(session_id=session_id, cwd="/home/user/project")
    discovery = _DiscoveryDouble(unbound)

    async def save_binding(binding) -> bool:
        store.save_binding(binding)
        return True

    async def remove_binding(actual_session_id: str, expected_binding_id: str | None):
        binding = store.get_binding(actual_session_id)
        if binding is None or binding.binding_id != expected_binding_id:
            return None
        store.remove_binding(actual_session_id)
        return binding

    save_callback = AsyncMock(side_effect=save_binding)
    remove_callback = AsyncMock(side_effect=remove_binding)
    binder = ExternalSessionBinder(
        discovery=discovery,  # type: ignore[arg-type]
        binding_store=store,
        projects_dir=Path("/tmp/projects"),
        save_callback=save_callback,
        remove_callback=remove_callback,
    )

    bind_result = await binder.bind(user_id=1, session_id=session_id)
    unbind_result = await binder.unbind(user_id=1, session_id=session_id)

    assert bind_result.success is True
    assert unbind_result.success is True
    save_callback.assert_awaited_once()
    saved_binding = save_callback.await_args.args[0]
    remove_callback.assert_awaited_once_with(session_id, saved_binding.binding_id)
    assert store.get_binding(session_id) is None


class _PidUnbound:
    """Unbound double carrying a live ``pid`` and an explicit ``tty``.

    Unlike ``_RaisingPidUnbound``, ``pid`` is a plain int so the binder can hand
    it to the process probe, and ``tty`` is controllable to exercise the
    bind-time tty backfill path.
    """

    def __init__(self, *, session_id: str, cwd: str, pid: int, tty: str | None) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.pid = pid
        self.tty = tty
        self.first_seen = datetime.now(UTC)
        self.last_seen = datetime.now(UTC)
        self.event_count = 1
        self.title = None


async def test_bind_backfills_tty_from_process_probe_when_unbound_tty_missing(tmp_path: Path) -> None:
    """When ``unbound.tty`` is None, bind resolves the controlling tty via the
    injected process probe and stores it on the binding as the input trust anchor."""
    session_id = "sess-tty-backfill"
    store = ExternalBindingStore(data_dir=tmp_path)
    unbound = _PidUnbound(session_id=session_id, cwd="/home/user/project", pid=24259, tty=None)
    discovery = _DiscoveryDouble(unbound)  # type: ignore[arg-type]
    probe = FakeLocalProcessProbe(tty="/dev/ttys006")

    binder = ExternalSessionBinder(
        discovery=discovery,  # type: ignore[arg-type]
        binding_store=store,
        projects_dir=Path("/tmp/projects"),
        sync_callback=None,
        process_probe=probe,  # type: ignore[arg-type]
    )

    result = await binder.bind(user_id=1, session_id=session_id)

    assert result.success is True
    assert probe.tty_calls == [24259]
    stored = store.get_binding(session_id)
    assert stored is not None
    assert stored.tty == "/dev/ttys006"


async def test_bind_keeps_unbound_tty_when_present_without_probing(tmp_path: Path) -> None:
    """When discovery already carries ``unbound.tty``, the probe is not consulted
    and the existing tty is preserved verbatim."""
    session_id = "sess-tty-present"
    store = ExternalBindingStore(data_dir=tmp_path)
    unbound = _PidUnbound(session_id=session_id, cwd="/home/user/project", pid=24259, tty="/dev/ttys010")
    discovery = _DiscoveryDouble(unbound)  # type: ignore[arg-type]
    probe = FakeLocalProcessProbe(tty="/dev/ttys006")

    binder = ExternalSessionBinder(
        discovery=discovery,  # type: ignore[arg-type]
        binding_store=store,
        projects_dir=Path("/tmp/projects"),
        sync_callback=None,
        process_probe=probe,  # type: ignore[arg-type]
    )

    result = await binder.bind(user_id=1, session_id=session_id)

    assert result.success is True
    assert probe.tty_calls == []  # not consulted because unbound.tty was set
    stored = store.get_binding(session_id)
    assert stored is not None
    assert stored.tty == "/dev/ttys010"


async def test_bind_leaves_tty_none_when_probe_returns_none(tmp_path: Path) -> None:
    """When both ``unbound.tty`` and the probe resolve to None, bind still
    succeeds with ``binding.tty is None`` (degrades, never blocks)."""
    session_id = "sess-tty-none"
    store = ExternalBindingStore(data_dir=tmp_path)
    unbound = _PidUnbound(session_id=session_id, cwd="/home/user/project", pid=24259, tty=None)
    discovery = _DiscoveryDouble(unbound)  # type: ignore[arg-type]
    probe = FakeLocalProcessProbe(tty=None)

    binder = ExternalSessionBinder(
        discovery=discovery,  # type: ignore[arg-type]
        binding_store=store,
        projects_dir=Path("/tmp/projects"),
        sync_callback=None,
        process_probe=probe,  # type: ignore[arg-type]
    )

    result = await binder.bind(user_id=1, session_id=session_id)

    assert result.success is True
    assert probe.tty_calls == [24259]
    stored = store.get_binding(session_id)
    assert stored is not None
    assert stored.tty is None
