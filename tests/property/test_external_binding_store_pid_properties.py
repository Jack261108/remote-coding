"""Property-based tests for ExternalBinding pid persistence and refresh.

Spec: external-binding-pid-liveness (feature), Tasks 4.2 and 4.3.

Two PBT properties, both synchronous (no async):
  Property 5 — pid serialization round-trip: saving an ExternalBinding with any
    pid value (None, 0, positive) and reloading from the same file produces an
    equal pid; pid=None serializes to explicit JSON null; an entry omitting the
    pid key loads as pid=None; pid=0 round-trips as 0 yet is Pid_Known=False.
  Property 7 — Hook event pid refresh always touches activity: touch_activity
    always updates last_activity_at to ``now`` in memory and updates pid iff the
    event pid is a positive integer, otherwise leaves the prior pid unchanged.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_store import ExternalBindingStore

# A fixed, tz-aware UTC bind time keeps construction deterministic across runs.
_FIXED_BOUND_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# --- Property 5: pid serialization round-trip -------------------------------


# Feature: external-binding-pid-liveness, Property 5: pid serialization round-trip
@settings(max_examples=100, deadline=None)
@given(pid=st.one_of(st.none(), st.just(0), st.integers(min_value=1)))
def test_property_5_pid_serialization_round_trip(pid: int | None) -> None:
    """Saving an ExternalBinding with any pid (None / 0 / positive) and reloading
    from the same file produces an equal pid. pid=None serializes to explicit
    JSON ``null`` (key present), an entry that OMITS the pid key loads as None,
    and a stored pid=0 round-trips as 0 while remaining Pid_Known=False.

    **Validates: Requirements 2.2, 2.3, 2.4, 2.5, 11.4**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        store = ExternalBindingStore(data_dir=tmp_path)

        session_id = "pbt-p5-session"
        store.save_binding(
            ExternalBinding(
                session_id=session_id,
                user_id=42,
                cwd="/home/user/project",
                bound_at=_FIXED_BOUND_AT,
                jsonl_path=None,
                pid=pid,
                last_activity_at_init=_FIXED_BOUND_AT,
            )
        )

        # Reload from disk via a brand-new store over the same dir (load_all).
        store2 = ExternalBindingStore(data_dir=tmp_path)
        reloaded = store2.get_binding(session_id)
        assert reloaded is not None, "binding should reload from disk"

        # Round-trip preservation of the pid value (Req 2.5).
        assert reloaded.pid == pid, f"reloaded pid ({reloaded.pid!r}) should equal original ({pid!r})"

        # pid=None must serialize to an explicit JSON null (key PRESENT) (Req 2.2).
        if pid is None:
            raw = json.loads((tmp_path / "external_bindings.json").read_text(encoding="utf-8"))
            entry = raw[session_id]
            assert "pid" in entry, "pid key must be present in serialized entry, not omitted"
            assert entry["pid"] is None, "pid=None must serialize as explicit JSON null"

        # A stored pid=0 round-trips as 0 yet is Pid_Known=False per the glossary
        # definition (pid is not None and pid > 0), so it falls to the idle path.
        if pid == 0:
            assert reloaded.pid == 0, "stored pid=0 must round-trip as 0"
            assert (reloaded.pid is not None and reloaded.pid > 0) is False, "pid=0 must be Pid_Known=False"

    # An entry that OMITS the pid key entirely must load as pid=None (Req 2.3,
    # 11.4). This is a fixed sub-assertion independent of the generated value.
    with tempfile.TemporaryDirectory() as tmp_dir2:
        tmp_path2 = Path(tmp_dir2)
        legacy_session_id = "pbt-p5-legacy"
        legacy_payload = {
            legacy_session_id: {
                "user_id": 7,
                "cwd": "/home/user/legacy",
                "bound_at": _FIXED_BOUND_AT.isoformat(),
                "last_activity_at": _FIXED_BOUND_AT.isoformat(),
                "jsonl_path": None,
                # NOTE: no "pid" key — simulates a pre-feature entry.
            }
        }
        (tmp_path2 / "external_bindings.json").write_text(
            json.dumps(legacy_payload),
            encoding="utf-8",
        )
        legacy_store = ExternalBindingStore(data_dir=tmp_path2)
        legacy_binding = legacy_store.get_binding(legacy_session_id)
        assert legacy_binding is not None, "legacy entry should load successfully"
        assert legacy_binding.pid is None, "entry omitting the pid key must load as pid=None"


# --- Property 7: Hook event pid refresh always touches activity -------------


# Feature: external-binding-pid-liveness, Property 7: Hook event pid refresh always touches activity
@settings(max_examples=100, deadline=None)
@given(
    prior_pid=st.integers(min_value=1),
    event_pid=st.one_of(st.none(), st.integers(max_value=0), st.integers(min_value=1)),
)
def test_property_7_hook_event_pid_refresh_always_touches_activity(
    prior_pid: int,
    event_pid: int | None,
) -> None:
    """For any tracked binding with a known prior (positive) pid and any event
    pid, ``touch_activity(session_id, now, pid=event_pid)`` ALWAYS sets
    ``last_activity_at`` to ``now`` in memory, and updates ``pid`` to
    ``event_pid`` iff ``event_pid is not None and event_pid > 0``; otherwise the
    existing prior pid is left unchanged.

    **Validates: Requirements 4.1, 4.2, 4.3**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        store = ExternalBindingStore(data_dir=tmp_path)

        session_id = "pbt-p7-session"
        store.save_binding(
            ExternalBinding(
                session_id=session_id,
                user_id=99,
                cwd="/home/user/proj",
                bound_at=_FIXED_BOUND_AT,
                jsonl_path=None,
                pid=prior_pid,
                last_activity_at_init=_FIXED_BOUND_AT,
            )
        )

        # A fresh timestamp distinct from the binding's current last_activity_at.
        now = utc_now() + timedelta(seconds=1)
        assert now != _FIXED_BOUND_AT

        store.touch_activity(session_id, now, pid=event_pid)

        binding = store.get_binding(session_id)
        assert binding is not None

        # last_activity_at is ALWAYS refreshed to ``now`` regardless of event pid.
        assert binding.last_activity_at == now, (
            f"last_activity_at ({binding.last_activity_at!r}) must equal now ({now!r}) regardless of event_pid ({event_pid!r})"
        )

        # pid is updated IFF event_pid is a positive integer; else unchanged.
        if event_pid is not None and event_pid > 0:
            assert binding.pid == event_pid, f"pid should update to positive event_pid ({event_pid!r})"
        else:
            assert binding.pid == prior_pid, f"pid should remain prior_pid ({prior_pid!r}) for event_pid={event_pid!r}"
