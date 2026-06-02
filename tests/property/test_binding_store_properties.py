"""Property-based tests for ExternalBindingStore persistence round-trip.

Feature: external-session-takeover, Property 8: Binding persistence round-trip

**Validates: Requirements 5.5**
"""

from __future__ import annotations

import tempfile
from datetime import UTC
from pathlib import Path

import hypothesis.strategies as st
from hypothesis import given, settings
from hypothesis.strategies import characters, datetimes, integers, just, none, one_of, text

from app.domain.external_session_models import ExternalBinding
from app.services.external_binding_store import ExternalBindingStore

# --- Strategies ---

session_id_st = text(
    min_size=1,
    max_size=50,
    alphabet=characters(whitelist_categories=("L", "N", "P")),
)

user_id_st = integers(min_value=1, max_value=10**9)

cwd_st = text(min_size=1, max_size=200)

bound_at_st = datetimes(timezones=just(UTC))

jsonl_path_st = one_of(none(), text(min_size=1, max_size=200))


binding_st = st.builds(
    ExternalBinding,
    session_id=session_id_st,
    user_id=user_id_st,
    cwd=cwd_st,
    bound_at=bound_at_st,
    jsonl_path=jsonl_path_st,
)


@settings(max_examples=100)
@given(bindings=st.lists(binding_st, min_size=0, max_size=20, unique_by=lambda b: b.session_id))
def test_binding_persistence_round_trip(bindings: list[ExternalBinding]) -> None:
    """Property 8: Binding persistence round-trip.

    For any set of active bindings, persisting them and then loading from a NEW
    store instance pointing to the same directory should produce an equivalent
    set of bindings.

    Feature: external-session-takeover, Property 8: Binding persistence round-trip
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Create store and save all bindings
        store = ExternalBindingStore(data_dir=tmp_path)
        for binding in bindings:
            store.save_binding(binding)

        # Create a NEW store instance pointing to the same directory (simulating restart)
        store2 = ExternalBindingStore(data_dir=tmp_path)
        loaded = store2.load_all()

        # Verify equivalence
        assert len(loaded) == len(bindings), f"Expected {len(bindings)} bindings, got {len(loaded)}"

        for binding in bindings:
            assert binding.session_id in loaded, f"Missing session_id {binding.session_id!r} after reload"
            reloaded = loaded[binding.session_id]
            assert reloaded.session_id == binding.session_id
            assert reloaded.user_id == binding.user_id
            assert reloaded.cwd == binding.cwd
            assert reloaded.bound_at == binding.bound_at
            assert reloaded.jsonl_path == binding.jsonl_path
