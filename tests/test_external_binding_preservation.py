"""Preservation baseline tests for stale external binding cleanup.

Spec: stale-external-binding-cleanup (bugfix)
Property 2: Preservation - Fresh and Protected Bindings Always Retained

**Observation-first methodology**: These tests exercise the EXISTING (unfixed)
`ExternalBinding` / `ExternalBindingStore` API and MUST PASS on the current
code. They capture the baseline behavior the fix must preserve:

  * No existing mechanism removes a binding automatically — bindings of ANY age
    (even far older than the future idle TTL) survive save + reload. This is the
    "active bindings are always preserved" baseline (no auto-cleanup exists yet).
  * The ONLY automatic removal path today is the `SessionEnd` handler, which
    calls `remove_binding(session_id)` immediately, regardless of binding age.
  * `remove_binding(session_id)` for a non-existent session_id is a graceful
    no-op (does not raise) and leaves other bindings untouched.

The existing `ExternalBinding` API has NO `last_activity_at` field, so `bound_at`
age is used as the proxy for "freshness" where needed.

**Validates: Requirements 2.4, 2.5, 3.1, 3.2, 3.4, 3.5**
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

import hypothesis.strategies as st
from hypothesis import given, settings
from hypothesis.strategies import characters, integers, none, one_of, text

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_store import ExternalBindingStore

# A TTL the fix will eventually introduce. Used here only to construct bindings
# that are "stale" relative to it, proving the UNFIXED store preserves them
# anyway (no cleanup mechanism exists today).
FUTURE_TTL = timedelta(hours=24)


# --- Strategies -------------------------------------------------------------

# JSON-key-safe, non-empty session ids (letters/numbers/punctuation).
session_id_st = text(
    min_size=1,
    max_size=50,
    alphabet=characters(whitelist_categories=("L", "N", "P")),
)

user_id_st = integers(min_value=1, max_value=10**9)

cwd_st = text(min_size=1, max_size=200)

jsonl_path_st = one_of(none(), text(min_size=1, max_size=200))

# Age (in hours) of a binding relative to now. Spans well past the future idle
# TTL (24h) on both sides so the property covers fresh AND stale bindings — the
# unfixed store must preserve them all.
age_hours_st = st.floats(min_value=0.0, max_value=240.0, allow_nan=False, allow_infinity=False)


@st.composite
def binding_st(draw: st.DrawFn) -> ExternalBinding:
    """Build an ExternalBinding with a controlled `bound_at` age."""
    age_hours = draw(age_hours_st)
    bound_at = utc_now() - timedelta(hours=age_hours)
    return ExternalBinding(
        session_id=draw(session_id_st),
        user_id=draw(user_id_st),
        cwd=draw(cwd_st),
        bound_at=bound_at,
        jsonl_path=draw(jsonl_path_st),
    )


# --- Unit tests -------------------------------------------------------------


def test_remove_nonexistent_binding_is_noop(tmp_path: Path) -> None:
    """`remove_binding` for an unknown session_id is a graceful no-op (Req 3.5)."""
    store = ExternalBindingStore(data_dir=tmp_path)

    # Should not raise even on an empty store.
    store.remove_binding("does-not-exist")

    assert store.get_binding("does-not-exist") is None

    # And it must not disturb existing bindings.
    keep = ExternalBinding(
        session_id="keep-me",
        user_id=1,
        cwd="/home/user/project",
        bound_at=utc_now(),
        jsonl_path=None,
    )
    store.save_binding(keep)
    store.remove_binding("still-not-here")
    assert store.get_binding("keep-me") is not None


def test_fresh_binding_preserved_save_then_get(tmp_path: Path) -> None:
    """A freshly created binding is retained: save then get returns it (Req 3.1)."""
    store = ExternalBindingStore(data_dir=tmp_path)
    binding = ExternalBinding(
        session_id="fresh-abc123",
        user_id=42,
        cwd="/home/user/project",
        bound_at=utc_now(),
        jsonl_path="/tmp/session.jsonl",
    )
    store.save_binding(binding)

    retrieved = store.get_binding("fresh-abc123")
    assert retrieved is not None
    assert retrieved.session_id == "fresh-abc123"
    assert retrieved.user_id == 42
    assert retrieved.cwd == "/home/user/project"
    assert retrieved.jsonl_path == "/tmp/session.jsonl"


def test_fresh_binding_preserved_after_reload_from_json(tmp_path: Path) -> None:
    """A fresh binding survives a restart: reload from JSON preserves it (Req 3.4)."""
    store = ExternalBindingStore(data_dir=tmp_path)
    binding = ExternalBinding(
        session_id="fresh-reload-xyz",
        user_id=7,
        cwd="/home/user/other",
        bound_at=utc_now(),
        jsonl_path=None,
    )
    store.save_binding(binding)

    # New store instance pointing at the same dir simulates a process restart.
    store2 = ExternalBindingStore(data_dir=tmp_path)
    reloaded = store2.get_binding("fresh-reload-xyz")
    assert reloaded is not None
    assert reloaded.user_id == 7
    assert reloaded.cwd == "/home/user/other"
    assert reloaded.bound_at == binding.bound_at


def test_stale_binding_also_preserved_by_existing_mechanisms(tmp_path: Path) -> None:
    """Baseline: the UNFIXED store has NO auto-cleanup, so even a binding far
    older than the future idle TTL survives save + reload untouched.

    This documents the pre-fix behavior the cleanup service will change.
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    stale = ExternalBinding(
        session_id="stale-but-kept",
        user_id=99,
        cwd="/home/user/stale",
        bound_at=utc_now() - (FUTURE_TTL + timedelta(hours=100)),
        jsonl_path=None,
    )
    store.save_binding(stale)

    # Still present in the same instance.
    assert store.get_binding("stale-but-kept") is not None

    # Still present after a simulated restart — nothing prunes it today.
    store2 = ExternalBindingStore(data_dir=tmp_path)
    assert store2.get_binding("stale-but-kept") is not None


def test_binding_saved_then_removed_via_remove_binding(tmp_path: Path) -> None:
    """The SessionEnd removal path: a saved binding can be removed via
    `remove_binding(session_id)` immediately, regardless of age (Req 3.2).
    """
    store = ExternalBindingStore(data_dir=tmp_path)
    binding = ExternalBinding(
        session_id="session-end-target",
        user_id=5,
        cwd="/home/user/proj",
        bound_at=utc_now(),  # fresh — removal does not wait for any TTL
        jsonl_path=None,
    )
    store.save_binding(binding)
    assert store.get_binding("session-end-target") is not None

    store.remove_binding("session-end-target")
    assert store.get_binding("session-end-target") is None

    # Removal persists across reload.
    store2 = ExternalBindingStore(data_dir=tmp_path)
    assert store2.get_binding("session-end-target") is None


# --- Property-based tests ---------------------------------------------------


@settings(max_examples=100)
@given(binding=binding_st())
def test_property_any_binding_preserved_by_store(binding: ExternalBinding) -> None:
    """Property 2 (preservation baseline): for ANY binding regardless of age,
    the existing store preserves it across save + reload — no mechanism removes
    it. Fresh and stale bindings alike are always retained.

    `bound_at` age is the proxy for freshness (no `last_activity_at` field yet).

    **Validates: Requirements 2.4, 3.1, 3.4**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        store = ExternalBindingStore(data_dir=tmp_path)
        store.save_binding(binding)

        # Retained in-memory.
        assert store.get_binding(binding.session_id) is not None

        # Retained after a simulated restart (reload from JSON).
        store2 = ExternalBindingStore(data_dir=tmp_path)
        reloaded = store2.get_binding(binding.session_id)
        assert reloaded is not None
        assert reloaded.session_id == binding.session_id
        assert reloaded.user_id == binding.user_id
        assert reloaded.cwd == binding.cwd
        assert reloaded.bound_at == binding.bound_at
        assert reloaded.jsonl_path == binding.jsonl_path


@settings(max_examples=100)
@given(binding=binding_st(), other_id=session_id_st)
def test_property_remove_unrelated_session_is_noop(binding: ExternalBinding, other_id: str) -> None:
    """Property 2 (preservation baseline): removing an unrelated session_id
    never removes a saved binding and never raises (Req 3.5).

    **Validates: Requirements 3.5**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        store = ExternalBindingStore(data_dir=tmp_path)
        store.save_binding(binding)

        if other_id != binding.session_id:
            store.remove_binding(other_id)  # must be a graceful no-op
            assert store.get_binding(binding.session_id) is not None
