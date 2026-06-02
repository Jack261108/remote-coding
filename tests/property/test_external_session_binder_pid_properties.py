"""Property-based test for bind-time pid propagation.

Spec: external-binding-pid-liveness (feature), Task 5.2.

Property 6 — Bind-time pid propagation: for any ``UnboundExternalSession`` with
pid value ``P`` (including ``None``), binding it produces an ``ExternalBinding``
whose stored ``pid`` equals ``P``.

Design references: requirements.md Req 3.1, 3.2; design.md "Correctness
Properties" Property 6 and "Components §4".
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.external_session_models import UnboundExternalSession
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder

# A fixed, tz-aware UTC timestamp keeps UnboundExternalSession construction
# deterministic across runs.
_FIXED_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class _DiscoveryDouble:
    """Minimal discovery test double.

    ``get`` returns the single configured ``UnboundExternalSession`` (carrying
    the generated pid); ``remove_session`` is a no-op so ``bind`` can complete
    its post-bind discovery cleanup without touching real discovery state.
    """

    def __init__(self, unbound: UnboundExternalSession) -> None:
        self._unbound = unbound

    def get(self, session_id: str) -> UnboundExternalSession | None:
        return self._unbound

    def remove_session(self, session_id: str) -> None:  # no-op
        return None


# Feature: external-binding-pid-liveness, Property 6: Bind-time pid propagation
@settings(max_examples=100, deadline=None)
@given(unbound_pid=st.one_of(st.none(), st.integers(min_value=1)))
def test_property_6_bind_time_pid_propagation(unbound_pid: int | None) -> None:
    """For any UnboundExternalSession.pid value P (including None), the
    ExternalBinding created by ``bind`` carries ``pid == P``.

    **Validates: Requirements 3.1, 3.2**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        store = ExternalBindingStore(data_dir=tmp_path)

        session_id = "pbt-p6-session"
        unbound = UnboundExternalSession(
            session_id=session_id,
            cwd="/home/user/project",
            pid=unbound_pid,
            first_seen=_FIXED_TS,
            last_seen=_FIXED_TS,
            event_count=1,
        )

        binder = ExternalSessionBinder(
            discovery=_DiscoveryDouble(unbound),
            binding_store=store,
            projects_dir=Path("/tmp/projects"),
            sync_callback=None,
        )

        result = asyncio.run(binder.bind(user_id=123, session_id=session_id))
        assert result.success is True, "bind should succeed for a discoverable session"

        binding = store.get_binding(session_id)
        assert binding is not None, "binding should be persisted in the store"
        assert binding.pid == unbound_pid, f"bound pid ({binding.pid!r}) should equal source UnboundExternalSession.pid ({unbound_pid!r})"
