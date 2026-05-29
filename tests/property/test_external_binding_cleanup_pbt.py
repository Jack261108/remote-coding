"""Property-based tests for stale external binding cleanup.

Spec: stale-external-binding-cleanup (bugfix), Task 8.7

Three PBT properties:
  A. Cleanup decision matches spec
     `idle_age > ttl AND NOT has_pending` -> remove; else retain.
  B. `touch_activity` sequence reflects the LAST call's timestamp
     In-memory (and persisted, with throttle disabled) `last_activity_at`
     equals the timestamp of the LAST touch, irrespective of ordering.
  C. JSON load round-trip with mixed timezone shapes
     Loaded `bound_at` / `last_activity_at` are tz-aware UTC; missing
     `last_activity_at` falls back to `bound_at`; absolute moments preserved.

**Validates: Requirements 2.1, 2.2, 2.4, 2.5, 2.7, 2.9**
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_cleanup_service import ExternalBindingCleanupService
from app.services.external_binding_store import ExternalBindingStore


# --- Shared helpers ---------------------------------------------------------


def _make_hook_server(*, has_pending: bool) -> AsyncMock:
    hook_server = AsyncMock()
    hook_server.has_pending_permission = AsyncMock(return_value=has_pending)
    hook_server.cancel_pending_permissions = AsyncMock(return_value=None)
    return hook_server


def _make_auto_approve_service() -> AsyncMock:
    auto = AsyncMock()
    auto.clear_session = AsyncMock(return_value=None)
    return auto


# --- Property A: Cleanup decision matches spec ------------------------------


@settings(max_examples=50, deadline=None)
@given(
    age_hours=st.integers(min_value=0, max_value=72),
    ttl_hours=st.integers(min_value=1, max_value=48),
    has_pending=st.booleans(),
)
@pytest.mark.asyncio
async def test_property_a_cleanup_decision_matches_spec(
    age_hours: int,
    ttl_hours: int,
    has_pending: bool,
) -> None:
    """For random `(age, ttl, has_pending)` tuples, `_cleanup()` removes the
    binding iff `age > ttl AND NOT has_pending`.

    Avoids the exact `age == ttl_hours` boundary because microsecond drift
    between the test's `utc_now()` call and the cleanup service's `utc_now()`
    call would push idle_age slightly past ttl, yielding flaky behavior right
    at the boundary. Integer-hour separation eliminates this drift sensitivity.

    **Validates: Requirements 2.1, 2.2, 2.4, 2.5**
    """
    assume(age_hours != ttl_hours)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        store = ExternalBindingStore(data_dir=tmp_path)

        now = utc_now()
        bound_at = now - timedelta(hours=age_hours)
        session_id = "pbt-a-session"
        store.save_binding(
            ExternalBinding(
                session_id=session_id,
                user_id=42,
                cwd="/home/user/project",
                bound_at=bound_at,
                jsonl_path=None,
                last_activity_at_init=bound_at,
            )
        )

        hook_server = _make_hook_server(has_pending=has_pending)
        auto_approve = _make_auto_approve_service()
        service = ExternalBindingCleanupService(
            binding_store=store,
            auto_approve_service=auto_approve,
            hook_socket_server=hook_server,
            ttl=timedelta(hours=ttl_hours),
            interval_sec=30.0,
        )

        await service._cleanup()

        is_stale_and_unprotected = age_hours > ttl_hours and not has_pending
        if is_stale_and_unprotected:
            assert store.get_binding(session_id) is None, (
                f"stale + unprotected binding should be removed "
                f"(age_hours={age_hours}, ttl_hours={ttl_hours}, has_pending={has_pending})"
            )
        else:
            assert store.get_binding(session_id) is not None, (
                f"fresh or protected binding should be retained "
                f"(age_hours={age_hours}, ttl_hours={ttl_hours}, has_pending={has_pending})"
            )


# --- Property B: touch_activity sequence reflects the LAST call -------------


_aware_utc_datetime_st = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)


@settings(max_examples=50, deadline=None)
@given(timestamps=st.lists(_aware_utc_datetime_st, min_size=1, max_size=20))
def test_property_b_touch_reflects_last_call(timestamps: list[datetime]) -> None:
    """A sequence of `touch_activity` calls leaves `last_activity_at` equal to
    the LAST call's timestamp — regardless of whether the timestamps are
    monotonically increasing, decreasing, or shuffled.

    `persist_min_interval_sec=0` is used so every call also persists, letting
    us verify the same property on the reloaded (persisted) state.

    **Validates: Requirements 2.7**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        store = ExternalBindingStore(data_dir=tmp_path)
        session_id = "pbt-b-session"
        store.save_binding(
            ExternalBinding(
                session_id=session_id,
                user_id=7,
                cwd="/home/user/proj",
                bound_at=utc_now(),
                jsonl_path=None,
            )
        )

        for ts in timestamps:
            store.touch_activity(session_id, ts, persist_min_interval_sec=0)

        # In-memory: last_activity_at MUST match the last call's timestamp.
        binding = store.get_binding(session_id)
        assert binding is not None
        assert binding.last_activity_at == timestamps[-1], (
            f"in-memory last_activity_at ({binding.last_activity_at!r}) " f"should equal last touch timestamp ({timestamps[-1]!r})"
        )

        # Persisted: with throttle disabled, every call hit disk too.
        # A fresh store instance reading the JSON sees the same value.
        store2 = ExternalBindingStore(data_dir=tmp_path)
        reloaded = store2.get_binding(session_id)
        assert reloaded is not None
        assert reloaded.last_activity_at == timestamps[-1], (
            f"persisted last_activity_at ({reloaded.last_activity_at!r}) " f"should equal last touch timestamp ({timestamps[-1]!r})"
        )


# --- Property C: JSON load normalizes mixed timezone shapes to UTC ----------


# Fixed-offset timezones (no DST ambiguity) for the "aware non-UTC" shape.
_non_utc_timezones = st.sampled_from([timezone(timedelta(hours=h)) for h in (-12, -8, -3, 2, 5, 9, 14)])

_naive_datetime_st = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    # No `timezones=` -> naive datetime.
)


@settings(max_examples=50, deadline=None)
@given(
    shape=st.sampled_from(["missing", "naive", "aware_non_utc"]),
    base_dt=_naive_datetime_st,
    activity_offset_sec=st.integers(min_value=0, max_value=60 * 60 * 24 * 30),
    tz=_non_utc_timezones,
)
def test_property_c_json_load_round_trip_mixed_timezone_shapes(
    shape: str,
    base_dt: datetime,
    activity_offset_sec: int,
    tz: timezone,
) -> None:
    """`ExternalBindingStore.load_all()` produces tz-aware UTC datetimes for
    every supported JSON shape (missing field, naive datetimes, aware non-UTC
    datetimes), preserving the absolute instant in time.

    **Validates: Requirements 2.9**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        session_id = "pbt-c-session"

        if shape == "missing":
            # Aware UTC `bound_at` written, `last_activity_at` field omitted.
            bound_at_aware = base_dt.replace(tzinfo=timezone.utc)
            payload_entry: dict = {
                "user_id": 42,
                "cwd": "/home/user/c",
                "bound_at": bound_at_aware.isoformat(),
                "jsonl_path": None,
            }
            expected_bound_ts = bound_at_aware.timestamp()
            expected_activity_ts = bound_at_aware.timestamp()  # falls back to bound_at
        elif shape == "naive":
            # Both naive in JSON (must be normalized as if UTC).
            last_activity_naive = base_dt + timedelta(seconds=activity_offset_sec)
            payload_entry = {
                "user_id": 42,
                "cwd": "/home/user/c",
                "bound_at": base_dt.isoformat(),
                "last_activity_at": last_activity_naive.isoformat(),
                "jsonl_path": None,
            }
            expected_bound_ts = base_dt.replace(tzinfo=timezone.utc).timestamp()
            expected_activity_ts = last_activity_naive.replace(tzinfo=timezone.utc).timestamp()
        else:  # "aware_non_utc"
            # Aware datetimes in a non-UTC fixed-offset timezone.
            bound_at_tz = base_dt.replace(tzinfo=tz)
            last_activity_tz = (base_dt + timedelta(seconds=activity_offset_sec)).replace(tzinfo=tz)
            payload_entry = {
                "user_id": 42,
                "cwd": "/home/user/c",
                "bound_at": bound_at_tz.isoformat(),
                "last_activity_at": last_activity_tz.isoformat(),
                "jsonl_path": None,
            }
            expected_bound_ts = bound_at_tz.timestamp()
            expected_activity_ts = last_activity_tz.timestamp()

        (tmp_path / "external_bindings.json").write_text(
            json.dumps({session_id: payload_entry}),
            encoding="utf-8",
        )

        store = ExternalBindingStore(data_dir=tmp_path)
        loaded = store.get_binding(session_id)
        assert loaded is not None, "binding should be loaded from JSON"

        # Both timestamps are timezone-aware UTC after load.
        assert loaded.bound_at.tzinfo is not None, "bound_at must be tz-aware after load"
        assert loaded.bound_at.utcoffset() == timedelta(0), "bound_at must be in UTC"
        assert loaded.last_activity_at.tzinfo is not None, "last_activity_at must be tz-aware after load"
        assert loaded.last_activity_at.utcoffset() == timedelta(0), "last_activity_at must be in UTC"

        # When `last_activity_at` was missing in JSON, it equals `bound_at`.
        if shape == "missing":
            assert loaded.last_activity_at == loaded.bound_at, "missing last_activity_at must fall back to bound_at"

        # Loaded UTC values represent the same absolute moment as the original.
        # Allow microsecond-scale tolerance for float round-trip safety.
        assert abs(loaded.bound_at.timestamp() - expected_bound_ts) < 1e-6, (
            f"bound_at instant changed under load: " f"{loaded.bound_at.timestamp()} vs expected {expected_bound_ts}"
        )
        assert abs(loaded.last_activity_at.timestamp() - expected_activity_ts) < 1e-6, (
            f"last_activity_at instant changed under load: " f"{loaded.last_activity_at.timestamp()} vs expected {expected_activity_ts}"
        )
