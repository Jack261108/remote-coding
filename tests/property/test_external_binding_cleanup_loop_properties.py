"""Property-based test for the cleanup loop's liveness governance.

Feature: external-binding-pid-liveness, Task 9.4

Covers the cleanup-loop correctness property from the design's "Correctness
Properties" section and "Components §7":

  - Property 4: Cleanup loop honors liveness governance

Target under test:
``app.services.external_binding_cleanup_service.ExternalBindingCleanupService._cleanup()``.

For any ``Pid_Known`` binding while ``liveness_enabled`` is true, the loop's
behavior is governed entirely by the liveness probe and is independent of idle
age and pending-permission state (Decision Matrix rows 1-3):

  - probe reports ALIVE  -> the reaper is never called, the binding is retained,
    and the pending-permission signal is never even consulted (row 1).
  - probe reports DEAD   -> the reaper is called exactly once with
    ``reason="pid_dead"``, regardless of idle age or pending permission, and the
    pending-permission signal is never consulted (rows 2, 3).

This test is structured as a SYNC Hypothesis test that drives the async
``_cleanup()`` via ``asyncio.run(...)`` (the repo runs pytest with
``asyncio_mode = "auto"``; an explicit ``asyncio.run`` here keeps the test free
of function-scoped-fixture health-check concerns and is the simplest faithful
realization of "one ``_cleanup()`` pass").

PATCH PATH (critical): the cleanup service binds the probe at import time via
``from app.services.process_liveness import process_is_alive``, so the loop
calls the module-local name. The patch therefore targets
``app.services.external_binding_cleanup_service.process_is_alive`` (the
consumer's binding), NOT ``app.services.process_liveness.process_is_alive``.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_cleanup_service import ExternalBindingCleanupService
from app.services.external_binding_store import ExternalBindingStore

_KNOWN_PID = 4242
_PROBE_PATH = "app.services.external_binding_cleanup_service.process_is_alive"


# Feature: external-binding-pid-liveness, Property 4: Cleanup loop honors liveness governance
@settings(max_examples=100, deadline=None)
@given(
    pid_alive=st.booleans(),
    idle_hours=st.integers(min_value=0, max_value=72),
    has_pending=st.booleans(),
)
def test_property_4_cleanup_loop_honors_liveness_governance(
    pid_alive: bool,
    idle_hours: int,
    has_pending: bool,
) -> None:
    """For any ``Pid_Known`` binding under ``liveness_enabled=True``, the cleanup
    loop is governed solely by the liveness probe:

      - ALIVE -> reaper called zero times, binding retained, pending signal never
        consulted (Decision Matrix row 1).
      - DEAD  -> reaper called exactly once with ``reason="pid_dead"``, regardless
        of idle age or pending permission, pending signal never consulted
        (rows 2, 3).

    The reaper is an ``AsyncMock`` (so it records calls without mutating the
    store), the probe is patched at the consumer's module binding, and the idle
    age / pending-permission inputs are varied to prove they do not influence the
    liveness-governed branch.

    **Validates: Requirements 5.1, 5.2, 6.1, 6.2, 6.3, 6.5**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ExternalBindingStore(data_dir=Path(tmp_dir))

        now = utc_now()
        session_id = "pbt-loop-session"
        store.save_binding(
            ExternalBinding(
                session_id=session_id,
                user_id=4242,
                cwd="/home/user/project",
                bound_at=now - timedelta(hours=idle_hours),
                jsonl_path=None,
                pid=_KNOWN_PID,
                last_activity_at_init=now - timedelta(hours=idle_hours),
            )
        )

        # Reaper is a mock so it records calls without mutating the store.
        reaper = AsyncMock()
        reaper.remove_with_cleanup = AsyncMock(return_value=True)

        # Hook socket server: in the Pid_Known + liveness-enabled branch the loop
        # short-circuits BEFORE consulting has_pending_permission.
        hook_socket_server = AsyncMock()
        hook_socket_server.has_pending_permission = AsyncMock(return_value=has_pending)

        service = ExternalBindingCleanupService(
            binding_store=store,
            hook_socket_server=hook_socket_server,
            reaper=reaper,
            liveness_enabled=True,
            ttl=timedelta(hours=24),
            interval_sec=30.0,
        )

        with patch(_PROBE_PATH, return_value=pid_alive):
            asyncio.run(service._cleanup())

        # Liveness governs: the pending-permission signal is never consulted on
        # the Pid_Known + liveness branch (the dead/alive verdict short-circuits
        # before the idle-TTL path's pending await).
        hook_socket_server.has_pending_permission.assert_not_awaited()

        if pid_alive:
            # Row 1: KEEP — reaper never called, binding still present, no removal,
            # regardless of idle age or pending permission.
            reaper.remove_with_cleanup.assert_not_awaited()
            assert store.get_binding(session_id) is not None, (
                f"an alive Pid_Known binding must be retained regardless of idle age (idle_hours={idle_hours}) or pending ({has_pending})"
            )
        else:
            # Rows 2-3: REMOVE — reaper called exactly once with reason='pid_dead',
            # regardless of idle age or pending permission.
            reaper.remove_with_cleanup.assert_awaited_once_with(session_id, reason="pid_dead")
