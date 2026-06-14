"""Property-based tests for SessionOwnershipResolver.

Feature: external-session-takeover
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app.domain.external_session_models import ExternalBinding, SessionOrigin
from app.domain.models import SessionContext
from app.services.external_binding_store import ExternalBindingStore
from app.services.session_ownership_resolver import SessionOwnershipResolver

# --- Strategies ---

session_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=4,
    max_size=40,
)

user_ids = st.integers(min_value=1, max_value=999999)

terminal_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=3,
    max_size=20,
)

workdirs = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="/-_"),
    min_size=2,
    max_size=60,
).map(lambda s: "/" + s.lstrip("/"))


def _make_context(
    *,
    user_id: int,
    claude_session_id: str,
    terminal_id: str | None = None,
    workdir: str = "/home/user/project",
) -> SessionContext:
    return SessionContext(
        user_id=user_id,
        session_id="internal-id",
        provider="claude_code",
        workdir=workdir,
        terminal_mode=terminal_id is not None,
        terminal_id=terminal_id,
        claude_session_id=claude_session_id,
    )


def _make_binding(session_id: str, user_id: int, cwd: str = "/tmp") -> ExternalBinding:
    return ExternalBinding(
        session_id=session_id,
        user_id=user_id,
        cwd=cwd,
        bound_at=datetime.now(UTC),
        jsonl_path=None,
    )


def _make_resolver(contexts: list[SessionContext], bindings: list[ExternalBinding] | None = None) -> SessionOwnershipResolver:
    """Create a resolver with given contexts and optional bindings."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ExternalBindingStore(data_dir=Path(tmp))
        for b in bindings or []:
            store.save_binding(b)

        svc = AsyncMock()
        svc.list_all = AsyncMock(return_value=contexts)

        # Create a lookup function that finds context by claude_session_id
        async def lookup_by_claude_session_id(session_id: str):
            for ctx in contexts:
                if ctx.claude_session_id == session_id:
                    return ctx
            return None

        svc.lookup_by_claude_session_id = AsyncMock(side_effect=lookup_by_claude_session_id)

        return SessionOwnershipResolver(session_service=svc, binding_store=store)


# --- Property 9: Ownership resolver priority chain ---


class TestOwnershipResolverPriorityChain:
    """Feature: external-session-takeover, Property 9: Ownership resolver priority chain

    Validates: Requirements 7.1, 7.2, 7.3, 7.4

    For any session_id, the ownership resolver should return results according
    to strict priority: tmux-owned > external-bound > unbound.
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        target_session_id=session_ids,
        tmux_user_id=user_ids,
        tmux_terminal_id=terminal_ids,
        binding_user_id=user_ids,
    )
    @pytest.mark.asyncio
    async def test_tmux_always_wins_over_binding(
        self,
        target_session_id: str,
        tmux_user_id: int,
        tmux_terminal_id: str,
        binding_user_id: int,
    ) -> None:
        """When both tmux ownership and external binding exist, tmux wins."""
        contexts = [
            _make_context(
                user_id=tmux_user_id,
                claude_session_id=target_session_id,
                terminal_id=tmux_terminal_id,
            ),
        ]
        bindings = [_make_binding(target_session_id, binding_user_id)]

        resolver = _make_resolver(contexts, bindings)
        result = await resolver.resolve(target_session_id)

        assert result.ownership_state == "owned"
        assert result.origin == SessionOrigin.TMUX
        assert result.owner_user_id == tmux_user_id

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        target_session_id=session_ids,
        binding_user_id=user_ids,
        binding_cwd=workdirs,
    )
    @pytest.mark.asyncio
    async def test_binding_wins_over_unbound(
        self,
        target_session_id: str,
        binding_user_id: int,
        binding_cwd: str,
    ) -> None:
        """When only external binding exists (no tmux), binding wins over unbound."""
        bindings = [_make_binding(target_session_id, binding_user_id, binding_cwd)]

        resolver = _make_resolver([], bindings)
        result = await resolver.resolve(target_session_id)

        assert result.ownership_state == "bound"
        assert result.origin == SessionOrigin.EXTERNAL
        assert result.owner_user_id == binding_user_id

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(target_session_id=session_ids)
    @pytest.mark.asyncio
    async def test_unbound_when_no_ownership(
        self,
        target_session_id: str,
    ) -> None:
        """When neither tmux nor binding exists, returns unbound."""
        resolver = _make_resolver([])
        result = await resolver.resolve(target_session_id)

        assert result.ownership_state == "unbound"
        assert result.origin == SessionOrigin.EXTERNAL
        assert result.owner_user_id is None

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        target_session_id=session_ids,
        tmux_user_id=user_ids,
        tmux_terminal_id=terminal_ids,
        other_session_ids=st.lists(session_ids, min_size=0, max_size=5),
        other_user_ids=st.lists(user_ids, min_size=0, max_size=5),
    )
    @pytest.mark.asyncio
    async def test_tmux_match_requires_both_session_id_and_terminal_id(
        self,
        target_session_id: str,
        tmux_user_id: int,
        tmux_terminal_id: str,
        other_session_ids: list[str],
        other_user_ids: list[int],
    ) -> None:
        """Tmux ownership requires both matching claude_session_id AND terminal_id != None."""
        contexts = []
        # The target session HAS terminal_id → should be owned
        contexts.append(
            _make_context(
                user_id=tmux_user_id,
                claude_session_id=target_session_id,
                terminal_id=tmux_terminal_id,
            )
        )
        # Other sessions without terminal_id that should NOT interfere
        for sid, uid in zip(other_session_ids, other_user_ids, strict=False):
            assume(sid != target_session_id)
            contexts.append(_make_context(user_id=uid, claude_session_id=sid, terminal_id=None))

        resolver = _make_resolver(contexts)
        result = await resolver.resolve(target_session_id)

        assert result.ownership_state == "owned"
        assert result.origin == SessionOrigin.TMUX
        assert result.owner_user_id == tmux_user_id


# --- Property 10: No workdir auto-bind for external sessions ---


class TestNoWorkdirAutoBind:
    """Feature: external-session-takeover, Property 10: No workdir auto-bind for external sessions

    Validates: Requirements 7.5

    For any unbound session whose cwd matches an existing user's workdir,
    the resolver should still return "unbound".
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        target_session_id=session_ids,
        user_id=user_ids,
        shared_workdir=workdirs,
    )
    @pytest.mark.asyncio
    async def test_matching_workdir_no_terminal_id_still_unbound(
        self,
        target_session_id: str,
        user_id: int,
        shared_workdir: str,
    ) -> None:
        """Session without terminal_id is unbound even if cwd matches a user's workdir."""
        contexts = [
            _make_context(
                user_id=user_id,
                claude_session_id="some-other-session",
                terminal_id=None,
                workdir=shared_workdir,
            ),
        ]

        resolver = _make_resolver(contexts)
        result = await resolver.resolve(target_session_id)

        assert result.ownership_state == "unbound"
        assert result.owner_user_id is None

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        target_session_id=session_ids,
        user_id=user_ids,
        shared_workdir=workdirs,
    )
    @pytest.mark.asyncio
    async def test_matching_session_id_but_no_terminal_still_unbound(
        self,
        target_session_id: str,
        user_id: int,
        shared_workdir: str,
    ) -> None:
        """Even if claude_session_id matches, without terminal_id it's still unbound.

        This is the critical property: a context with matching session_id but
        no terminal_id must NOT be treated as tmux-owned.
        """
        contexts = [
            _make_context(
                user_id=user_id,
                claude_session_id=target_session_id,
                terminal_id=None,
                workdir=shared_workdir,
            ),
        ]

        resolver = _make_resolver(contexts)
        result = await resolver.resolve(target_session_id)

        assert result.ownership_state == "unbound"
        assert result.owner_user_id is None

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        target_session_id=session_ids,
        user_ids_list=st.lists(user_ids, min_size=1, max_size=5),
        shared_workdir=workdirs,
    )
    @pytest.mark.asyncio
    async def test_multiple_users_same_workdir_still_unbound(
        self,
        target_session_id: str,
        user_ids_list: list[int],
        shared_workdir: str,
    ) -> None:
        """Multiple users with same workdir, none with terminal_id → still unbound."""
        contexts = [
            _make_context(
                user_id=uid,
                claude_session_id=f"other-{i}",
                terminal_id=None,
                workdir=shared_workdir,
            )
            for i, uid in enumerate(user_ids_list)
        ]

        resolver = _make_resolver(contexts)
        result = await resolver.resolve(target_session_id)

        assert result.ownership_state == "unbound"
        assert result.owner_user_id is None
