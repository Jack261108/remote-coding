"""Unit tests for ExternalBindingStore pid handling.

Spec: external-binding-pid-liveness (feature), Task 4.4

Covers three behaviors of ``ExternalBindingStore`` related to the new ``pid``
field and the pre-existing throttle / load / removal contracts:

  * Throttle (Req 4.5)  - a pid change within the per-session persist throttle
    window stays in memory only (no extra disk write) and is persisted on the
    next throttle-eligible touch.
  * Migration (Req 11.4) - a pre-feature ``external_bindings.json`` with no
    ``pid`` keys loads every entry with ``pid = None``; an entry with a
    malformed REQUIRED field still flows through the existing error path so the
    whole load resolves to an empty store (no silent coercion).
  * No-op removal (Req 11.5) - ``remove_binding`` for an unknown ``session_id``
    is a no-op: it does not raise and leaves existing bindings untouched.

These tests target ``app/services/external_binding_store.py`` ->
``ExternalBindingStore`` and use the ``tmp_path`` fixture for ``data_dir``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_store import ExternalBindingStore

# --- Helpers ----------------------------------------------------------------


def _write_bindings_json(data_dir: Path, payload: dict[str, dict]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "external_bindings.json").write_text(json.dumps(payload), encoding="utf-8")


def _read_bindings_json(data_dir: Path) -> dict[str, dict]:
    return json.loads((data_dir / "external_bindings.json").read_text(encoding="utf-8"))


# --- Throttle (Req 4.5) ------------------------------------------------------


def test_pid_change_within_throttle_stays_in_memory_then_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pid change within the throttle window is memory-only; it persists on
    the next throttle-eligible touch.

    Validates: Requirements 4.5

    ``save_binding`` pops ``_last_persist_at`` for the session, so the FIRST
    ``touch_activity`` after a save persists immediately and records a fresh
    throttle timestamp. A SECOND touch within ``persist_min_interval_sec`` must
    update memory only (no extra ``_persist`` call), and a later
    throttle-eligible touch must flush the in-memory pid to disk. ``_persist``
    is spied (call counting) and ``time.monotonic`` is pinned so the throttle
    decision is fully deterministic and independent of wall-clock timing.
    """
    # Pin monotonic so the throttle decision never depends on real elapsed time.
    monkeypatch.setattr(
        "app.services.external_binding_store.time.monotonic",
        lambda: 1_000.0,
    )

    bound_at = utc_now() - timedelta(hours=1)
    binding = ExternalBinding(
        session_id="session-throttle",
        user_id=42,
        cwd="/home/user/project",
        bound_at=bound_at,
        jsonl_path=None,
        pid=None,
    )
    store = ExternalBindingStore(data_dir=tmp_path)
    store.save_binding(binding)  # persists once; pops the throttle timestamp.

    # Spy on _persist AFTER the save so we only count touch-driven writes.
    persist_calls = {"count": 0}
    original_persist = store._persist

    def counting_persist() -> None:
        persist_calls["count"] += 1
        original_persist()

    monkeypatch.setattr(store, "_persist", counting_persist)

    # First touch after save: throttle entry was popped -> persists immediately.
    first_activity = utc_now()
    store.touch_activity("session-throttle", first_activity, pid=111, persist_min_interval_sec=60)
    assert persist_calls["count"] == 1, "first touch after save SHALL persist immediately"
    assert _read_bindings_json(tmp_path)["session-throttle"]["pid"] == 111

    # Second touch within the throttle window: memory-only, no disk write.
    second_activity = utc_now() + timedelta(seconds=1)
    store.touch_activity("session-throttle", second_activity, pid=222, persist_min_interval_sec=60)
    assert persist_calls["count"] == 1, "throttled touch must NOT write to disk"

    in_memory = store.get_binding("session-throttle")
    assert in_memory is not None
    assert in_memory.pid == 222, "in-memory pid update is always immediate"
    assert _read_bindings_json(tmp_path)["session-throttle"]["pid"] == 111, "on-disk pid must still be the previously persisted value"

    # Throttle-eligible touch (interval 0 forces a persist): flush memory to disk.
    third_activity = utc_now() + timedelta(seconds=2)
    store.touch_activity("session-throttle", third_activity, persist_min_interval_sec=0)
    assert persist_calls["count"] == 2, "throttle-eligible touch SHALL persist again"
    assert _read_bindings_json(tmp_path)["session-throttle"]["pid"] == 222, (
        "the in-memory pid change is persisted on the next eligible touch"
    )

    # A fresh store reading the same file observes the persisted pid.
    reloaded = ExternalBindingStore(data_dir=tmp_path).get_binding("session-throttle")
    assert reloaded is not None
    assert reloaded.pid == 222


# --- Migration (Req 11.4) ----------------------------------------------------


def test_pre_feature_json_without_pid_loads_every_entry_with_pid_none(tmp_path: Path) -> None:
    """A pre-feature bindings file with no ``pid`` keys loads every entry with
    ``pid = None``.

    Validates: Requirements 11.4

    Entries written before this feature simply omit the ``pid`` key; the store
    must load them all successfully and assign ``pid = None`` rather than
    failing or coercing.
    """
    bound_at = utc_now() - timedelta(hours=2)
    valid_dir = tmp_path / "valid"
    _write_bindings_json(
        valid_dir,
        {
            "session-a": {
                "user_id": 1,
                "cwd": "/home/user/a",
                "bound_at": bound_at.isoformat(),
                "jsonl_path": None,
            },
            "session-b": {
                "user_id": 2,
                "cwd": "/home/user/b",
                "bound_at": bound_at.isoformat(),
                "last_activity_at": bound_at.isoformat(),
                "jsonl_path": "/home/user/b/session.jsonl",
            },
        },
    )

    store = ExternalBindingStore(data_dir=valid_dir)
    loaded = store.list_all()

    assert {b.session_id for b in loaded} == {"session-a", "session-b"}
    assert all(b.pid is None for b in loaded), "entries omitting pid must load with pid None"


def test_malformed_required_field_flows_through_existing_error_path(tmp_path: Path) -> None:
    """An entry with a malformed REQUIRED field is handled by the existing
    error path: the whole load resolves to an empty store.

    Validates: Requirements 11.4

    A missing required field (here ``user_id``) raises inside ``load_all``,
    which is caught by the existing ``except (JSONDecodeError, KeyError,
    ValueError, OSError)`` handler that logs and returns ``{}``. The malformed
    data must NOT be silently coerced into a partial binding.
    """
    bound_at = utc_now() - timedelta(hours=2)
    malformed_dir = tmp_path / "malformed"
    _write_bindings_json(
        malformed_dir,
        {
            "session-bad": {
                # "user_id" intentionally omitted -> KeyError on load.
                "cwd": "/home/user/bad",
                "bound_at": bound_at.isoformat(),
                "jsonl_path": None,
            },
        },
    )

    store = ExternalBindingStore(data_dir=malformed_dir)

    assert store.list_all() == [], "malformed required field must yield an empty store"
    assert store.get_binding("session-bad") is None


# --- No-op removal (Req 11.5) ------------------------------------------------


def test_remove_binding_unknown_session_is_a_no_op(tmp_path: Path) -> None:
    """``remove_binding`` for an unknown session_id is a no-op.

    Validates: Requirements 11.5

    The call must not raise and must leave existing bindings untouched.
    """
    binding = ExternalBinding(
        session_id="session-known",
        user_id=7,
        cwd="/home/user/known",
        bound_at=utc_now(),
        jsonl_path=None,
        pid=4242,
    )
    store = ExternalBindingStore(data_dir=tmp_path)
    store.save_binding(binding)

    # Removing a session that was never stored must not raise.
    store.remove_binding("session-does-not-exist")

    remaining = store.list_all()
    assert len(remaining) == 1
    assert remaining[0].session_id == "session-known"
    assert remaining[0].pid == 4242
