"""Unit tests for ExternalBinding and ExternalBindingStore changes.

Spec: stale-external-binding-cleanup (bugfix), Task 8.3

Covers test plan items T1-T5c and T13b from the bugfix test plan
(.kiro/specs/stale-external-binding-cleanup/bugfix.md):

  * T1   - load JSON missing `last_activity_at` -> falls back to `bound_at`
  * T2   - save + reload preserves `last_activity_at` (round-trip)
  * T3   - construction without `last_activity_at_init` defaults to
           `bound_at`; with explicit value, it is preserved
  * T4   - naive `bound_at` in JSON is normalized to tz-aware UTC on load
  * T4b  - naive `last_activity_at` in JSON is normalized to tz-aware UTC
  * T4c  - aware non-UTC datetime in JSON is converted to UTC on load
           (same absolute instant, `.utcoffset() == timedelta(0)`)
  * T4d  - missing `last_activity_at` AND naive `bound_at` are both
           normalized; `last_activity_at` falls back to the normalized
           `bound_at`
  * T5   - `touch_activity` updates `binding.last_activity_at` in memory
           immediately
  * T5b  - two `touch_activity` calls within `persist_min_interval_sec`
           cause only the first to hit disk
  * T5c  - after the throttle window elapses, the next `touch_activity`
           persists again
  * T13b - `list_all()` returns a snapshot; iterating it while
           `remove_binding` mutates the underlying dict does not raise
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_store import ExternalBindingStore


# --- Helpers ----------------------------------------------------------------


def _write_bindings_json(data_dir: Path, payload: dict[str, dict]) -> None:
    (data_dir / "external_bindings.json").write_text(json.dumps(payload), encoding="utf-8")


def _read_bindings_json(data_dir: Path) -> dict[str, dict]:
    return json.loads((data_dir / "external_bindings.json").read_text(encoding="utf-8"))


# --- T1: missing last_activity_at -> fallback to bound_at -------------------


def test_t1_load_json_missing_last_activity_at_falls_back_to_bound_at(tmp_path: Path) -> None:
    bound_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_bindings_json(
        tmp_path,
        {
            "session-t1": {
                "user_id": 42,
                "cwd": "/home/user/project",
                "bound_at": bound_at.isoformat(),
                "jsonl_path": None,
            }
        },
    )

    store = ExternalBindingStore(data_dir=tmp_path)
    loaded = store.get_binding("session-t1")

    assert loaded is not None
    assert loaded.bound_at == bound_at
    assert loaded.last_activity_at == bound_at, "missing last_activity_at must fall back to bound_at"


# --- T2: round-trip preserves last_activity_at -------------------------------


def test_t2_save_and_reload_preserves_last_activity_at(tmp_path: Path) -> None:
    bound_at = utc_now() - timedelta(hours=2)
    last_activity = utc_now() - timedelta(minutes=5)
    binding = ExternalBinding(
        session_id="session-t2",
        user_id=7,
        cwd="/home/user/proj",
        bound_at=bound_at,
        jsonl_path=None,
        last_activity_at_init=last_activity,
    )

    store = ExternalBindingStore(data_dir=tmp_path)
    store.save_binding(binding)

    store2 = ExternalBindingStore(data_dir=tmp_path)
    reloaded = store2.get_binding("session-t2")

    assert reloaded is not None
    assert reloaded.bound_at == bound_at
    assert reloaded.last_activity_at == last_activity


# --- T3: dataclass default and explicit override ----------------------------


def test_t3_default_last_activity_at_equals_bound_at() -> None:
    bound_at = utc_now()
    binding = ExternalBinding(
        session_id="session-t3-default",
        user_id=1,
        cwd="/home/user/x",
        bound_at=bound_at,
        jsonl_path=None,
    )

    assert binding.last_activity_at == bound_at


def test_t3_explicit_last_activity_at_is_preserved() -> None:
    bound_at = utc_now() - timedelta(hours=10)
    explicit = utc_now() - timedelta(minutes=30)
    binding = ExternalBinding(
        session_id="session-t3-explicit",
        user_id=1,
        cwd="/home/user/x",
        bound_at=bound_at,
        jsonl_path=None,
        last_activity_at_init=explicit,
    )

    assert binding.last_activity_at == explicit
    assert binding.bound_at == bound_at


# --- T4: naive bound_at in JSON normalized to tz-aware UTC ------------------


def test_t4_naive_bound_at_in_json_normalized_to_aware_utc(tmp_path: Path) -> None:
    naive_bound = datetime(2026, 5, 1, 9, 0, 0)  # no tzinfo
    last_activity = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    _write_bindings_json(
        tmp_path,
        {
            "session-t4": {
                "user_id": 9,
                "cwd": "/home/user/c",
                "bound_at": naive_bound.isoformat(),
                "last_activity_at": last_activity.isoformat(),
                "jsonl_path": None,
            }
        },
    )

    store = ExternalBindingStore(data_dir=tmp_path)
    loaded = store.get_binding("session-t4")
    assert loaded is not None

    assert loaded.bound_at.tzinfo is not None
    assert loaded.bound_at.utcoffset() == timedelta(0)
    # Naive should be treated as UTC, so the absolute instant matches.
    assert loaded.bound_at == naive_bound.replace(tzinfo=timezone.utc)


# --- T4b: naive last_activity_at in JSON normalized to tz-aware UTC ---------


def test_t4b_naive_last_activity_at_in_json_normalized_to_aware_utc(tmp_path: Path) -> None:
    bound_at = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)
    naive_activity = datetime(2026, 5, 1, 11, 0, 0)  # no tzinfo
    _write_bindings_json(
        tmp_path,
        {
            "session-t4b": {
                "user_id": 9,
                "cwd": "/home/user/c",
                "bound_at": bound_at.isoformat(),
                "last_activity_at": naive_activity.isoformat(),
                "jsonl_path": None,
            }
        },
    )

    store = ExternalBindingStore(data_dir=tmp_path)
    loaded = store.get_binding("session-t4b")
    assert loaded is not None

    assert loaded.last_activity_at.tzinfo is not None
    assert loaded.last_activity_at.utcoffset() == timedelta(0)
    assert loaded.last_activity_at == naive_activity.replace(tzinfo=timezone.utc)


# --- T4c: aware non-UTC datetime in JSON converted to UTC -------------------


def test_t4c_aware_non_utc_datetime_in_json_converted_to_utc(tmp_path: Path) -> None:
    plus_eight = timezone(timedelta(hours=8))
    bound_at_plus8 = datetime(2026, 6, 1, 17, 0, 0, tzinfo=plus_eight)  # 09:00 UTC
    last_activity_plus8 = datetime(2026, 6, 1, 18, 0, 0, tzinfo=plus_eight)  # 10:00 UTC
    _write_bindings_json(
        tmp_path,
        {
            "session-t4c": {
                "user_id": 9,
                "cwd": "/home/user/c",
                "bound_at": bound_at_plus8.isoformat(),
                "last_activity_at": last_activity_plus8.isoformat(),
                "jsonl_path": None,
            }
        },
    )

    store = ExternalBindingStore(data_dir=tmp_path)
    loaded = store.get_binding("session-t4c")
    assert loaded is not None

    # Loaded values are UTC.
    assert loaded.bound_at.utcoffset() == timedelta(0)
    assert loaded.last_activity_at.utcoffset() == timedelta(0)
    # And represent the same absolute instant as the original.
    assert loaded.bound_at.timestamp() == pytest.approx(bound_at_plus8.timestamp(), abs=1e-6)
    assert loaded.last_activity_at.timestamp() == pytest.approx(last_activity_plus8.timestamp(), abs=1e-6)


# --- T4d: missing last_activity_at + naive bound_at -------------------------


def test_t4d_missing_last_activity_at_with_naive_bound_at(tmp_path: Path) -> None:
    naive_bound = datetime(2026, 7, 1, 12, 0, 0)
    _write_bindings_json(
        tmp_path,
        {
            "session-t4d": {
                "user_id": 9,
                "cwd": "/home/user/c",
                "bound_at": naive_bound.isoformat(),
                "jsonl_path": None,
                # no last_activity_at field
            }
        },
    )

    store = ExternalBindingStore(data_dir=tmp_path)
    loaded = store.get_binding("session-t4d")
    assert loaded is not None

    # Both are now tz-aware UTC.
    assert loaded.bound_at.tzinfo is not None
    assert loaded.bound_at.utcoffset() == timedelta(0)
    assert loaded.last_activity_at.tzinfo is not None
    assert loaded.last_activity_at.utcoffset() == timedelta(0)
    # And last_activity_at falls back to the normalized bound_at.
    assert loaded.last_activity_at == loaded.bound_at
    assert loaded.bound_at == naive_bound.replace(tzinfo=timezone.utc)


# --- T5: touch_activity updates in-memory immediately -----------------------


def test_t5_touch_activity_updates_memory_immediately(tmp_path: Path) -> None:
    bound_at = utc_now() - timedelta(hours=1)
    binding = ExternalBinding(
        session_id="session-t5",
        user_id=42,
        cwd="/home/user/x",
        bound_at=bound_at,
        jsonl_path=None,
    )
    store = ExternalBindingStore(data_dir=tmp_path)
    store.save_binding(binding)

    new_activity = utc_now()
    store.touch_activity("session-t5", new_activity)

    in_memory = store.get_binding("session-t5")
    assert in_memory is not None
    assert in_memory.last_activity_at == new_activity


# --- T5b/T5c: throttled persistence -----------------------------------------


def test_t5b_two_touches_within_throttle_window_only_first_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_now = [1_000.0]
    monkeypatch.setattr(
        "app.services.external_binding_store.time.monotonic",
        lambda: fake_now[0],
    )

    bound_at = utc_now() - timedelta(hours=1)
    binding = ExternalBinding(
        session_id="session-t5b",
        user_id=42,
        cwd="/home/user/x",
        bound_at=bound_at,
        jsonl_path=None,
    )
    store = ExternalBindingStore(data_dir=tmp_path)
    store.save_binding(binding)
    # save_binding pops _last_persist_at, so the next touch is the "first"
    # touch since save and SHALL persist immediately.

    first_activity = utc_now()
    store.touch_activity("session-t5b", first_activity, persist_min_interval_sec=60)

    persisted_after_first = _read_bindings_json(tmp_path)["session-t5b"]
    assert persisted_after_first["last_activity_at"] == first_activity.isoformat(), "first touch after save SHALL persist immediately"

    # Second touch within the throttle window: in-memory updates, disk does NOT.
    fake_now[0] += 30.0  # < 60s
    second_activity = utc_now() + timedelta(seconds=30)
    store.touch_activity("session-t5b", second_activity, persist_min_interval_sec=60)

    in_memory = store.get_binding("session-t5b")
    assert in_memory is not None
    assert in_memory.last_activity_at == second_activity, "in-memory update is always immediate"

    persisted_after_second = _read_bindings_json(tmp_path)["session-t5b"]
    assert persisted_after_second["last_activity_at"] == first_activity.isoformat(), "second touch within throttle window must NOT persist"


def test_t5c_touch_after_throttle_window_persists_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_now = [1_000.0]
    monkeypatch.setattr(
        "app.services.external_binding_store.time.monotonic",
        lambda: fake_now[0],
    )

    bound_at = utc_now() - timedelta(hours=1)
    binding = ExternalBinding(
        session_id="session-t5c",
        user_id=42,
        cwd="/home/user/x",
        bound_at=bound_at,
        jsonl_path=None,
    )
    store = ExternalBindingStore(data_dir=tmp_path)
    store.save_binding(binding)

    first = utc_now()
    store.touch_activity("session-t5c", first, persist_min_interval_sec=60)

    # Inside throttle: should not persist.
    fake_now[0] += 30.0
    second = utc_now() + timedelta(seconds=30)
    store.touch_activity("session-t5c", second, persist_min_interval_sec=60)

    persisted = _read_bindings_json(tmp_path)["session-t5c"]
    assert persisted["last_activity_at"] == first.isoformat()

    # Advance past the window: the next touch persists.
    fake_now[0] += 31.0  # total elapsed since first persist == 61s
    third = utc_now() + timedelta(seconds=120)
    store.touch_activity("session-t5c", third, persist_min_interval_sec=60)

    persisted = _read_bindings_json(tmp_path)["session-t5c"]
    assert persisted["last_activity_at"] == third.isoformat(), "touch after throttle window SHALL persist again"


# --- T13b: list_all() snapshot is iteration-safe under mutation -------------


def test_t13b_list_all_is_a_snapshot_safe_under_concurrent_mutation(tmp_path: Path) -> None:
    store = ExternalBindingStore(data_dir=tmp_path)
    bound_at = utc_now()

    for i in range(5):
        store.save_binding(
            ExternalBinding(
                session_id=f"snap-{i}",
                user_id=i,
                cwd=f"/home/user/snap{i}",
                bound_at=bound_at,
                jsonl_path=None,
            )
        )

    iterated = []
    snapshot = store.list_all()
    for binding in snapshot:
        # Mutate the underlying store mid-iteration. This must not raise; the
        # snapshot's view of `_bindings` is decoupled from the live dict.
        store.remove_binding(binding.session_id)
        iterated.append(binding.session_id)

    assert sorted(iterated) == [f"snap-{i}" for i in range(5)]
    assert store.list_all() == [], "all bindings removed during iteration"
