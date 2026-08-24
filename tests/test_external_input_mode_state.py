"""Unit tests for ExternalInputTargetStore (in-process input-mode state)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.external_input_mode_state import (
    ActiveExternalInputTarget,
    ExternalInputTargetStore,
)


async def test_set_and_get_target() -> None:
    store = ExternalInputTargetStore()
    target = await store.set_target(user_id=42, session_id="s1", binding_id="gen-1")
    assert target.user_id == 42 and target.session_id == "s1" and target.binding_id == "gen-1"
    got = await store.get_target(42)
    assert got is target


async def test_replacing_target_supersedes_prior() -> None:
    """Selecting session B while session A was active replaces the intent —
    no cross-session routing ambiguity."""
    store = ExternalInputTargetStore()
    await store.set_target(user_id=42, session_id="sA", binding_id="genA")
    new = await store.set_target(user_id=42, session_id="sB", binding_id="genB")
    got = await store.get_target(42)
    assert got is new and got.session_id == "sB"


async def test_clear_target_returns_cleared_or_none() -> None:
    store = ExternalInputTargetStore()
    assert await store.clear_target(42) is None
    set_t = await store.set_target(user_id=42, session_id="s1", binding_id="gen-1")
    cleared = await store.clear_target(42)
    assert cleared is set_t
    assert await store.get_target(42) is None


async def test_clear_target_for_session_clears_all_users() -> None:
    """Multiple users may have selected the same session; unbind/SessionEnd
    clears every one of them."""
    store = ExternalInputTargetStore()
    await store.set_target(user_id=1, session_id="s1", binding_id="gen-1")
    await store.set_target(user_id=2, session_id="s1", binding_id="gen-1")
    await store.set_target(user_id=3, session_id="s2", binding_id="gen-2")
    removed = await store.clear_target_for_session("s1")
    assert sorted(t.user_id for t in removed) == [1, 2]
    # s2 untouched, s1 gone for both users.
    assert await store.get_target(3) is not None
    assert await store.get_target(1) is None
    assert await store.get_target(2) is None


async def test_invalidate_for_binding_aba_drops_stale_generation_only() -> None:
    """After unbind+rebind produced gen-2, a target still bound to gen-1 is
    dropped; a target on a different session is untouched; a target on the
    same session with the live generation is left alone."""
    store = ExternalInputTargetStore()
    await store.set_target(user_id=1, session_id="s1", binding_id="gen-1")
    await store.set_target(user_id=2, session_id="s2", binding_id="gen-x")
    await store.set_target(user_id=3, session_id="s1", binding_id="gen-2")
    removed = await store.invalidate_for_binding_aba("s1", "gen-2")
    assert [t.user_id for t in removed] == [1]
    assert await store.get_target(1) is None
    assert await store.get_target(2) is not None
    assert await store.get_target(3) is not None


async def test_target_is_frozen() -> None:
    import dataclasses

    t = ActiveExternalInputTarget(user_id=1, session_id="s", binding_id="g", selected_at=datetime.now(UTC))
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.session_id = "x"  # type: ignore[misc]


async def test_all_targets_snapshot() -> None:
    store = ExternalInputTargetStore()
    await store.set_target(user_id=1, session_id="s1", binding_id="g1")
    await store.set_target(user_id=2, session_id="s2", binding_id="g2")
    snap = await store.all_targets()
    assert sorted(t.user_id for t in snap) == [1, 2]


async def test_selected_at_uses_injected_clock() -> None:
    fixed = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    store = ExternalInputTargetStore(now=lambda: fixed)
    target = await store.set_target(user_id=42, session_id="s1", binding_id="g")
    assert target.selected_at == fixed
