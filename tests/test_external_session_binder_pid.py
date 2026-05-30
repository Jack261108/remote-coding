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

from datetime import datetime, timezone
from pathlib import Path

from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder


class _RaisingPidUnbound:
    """Stand-in for ``UnboundExternalSession`` whose ``pid`` access raises.

    Exposes the attributes ``ExternalSessionBinder.bind`` reads from the unbound
    session (``session_id`` and ``cwd``) as plain attributes, while ``pid`` is a
    property that raises to simulate an internal error / race at capture time.
    """

    def __init__(self, *, session_id: str, cwd: str) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.first_seen = datetime.now(timezone.utc)
        self.last_seen = datetime.now(timezone.utc)
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
