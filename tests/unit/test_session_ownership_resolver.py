"""Unit tests for SessionOwnershipResolver."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.external_session_models import ExternalBinding, SessionOrigin
from app.services.external_binding_store import ExternalBindingStore
from app.services.session_ownership_resolver import SessionOwnershipResolver
from tests.fakes.external_session import FakeSessionService
from tests.fakes.external_session import make_session_context as _make_context


@pytest.fixture
def binding_store(tmp_path: Path) -> ExternalBindingStore:
    return ExternalBindingStore(data_dir=tmp_path)


@pytest.fixture
def session_service() -> FakeSessionService:
    return FakeSessionService()


@pytest.fixture
def resolver(session_service: FakeSessionService, binding_store: ExternalBindingStore) -> SessionOwnershipResolver:
    return SessionOwnershipResolver(
        session_service=session_service,
        binding_store=binding_store,
    )


@pytest.mark.asyncio
async def test_resolve_tmux_owned(resolver: SessionOwnershipResolver, session_service: FakeSessionService) -> None:
    """Session with terminal_id and matching claude_session_id is tmux-owned."""
    session_service.contexts = [
        _make_context(user_id=42, claude_session_id="sess-abc", terminal_id="term-1"),
    ]

    result = await resolver.resolve("sess-abc")

    assert result.ownership_state == "owned"
    assert result.origin == SessionOrigin.TMUX
    assert result.owner_user_id == 42


@pytest.mark.asyncio
async def test_resolve_external_bound(
    resolver: SessionOwnershipResolver,
    session_service: FakeSessionService,
    binding_store: ExternalBindingStore,
) -> None:
    """Session in binding store is externally bound."""
    session_service.contexts = []
    binding = ExternalBinding(
        session_id="sess-ext",
        user_id=99,
        cwd="/tmp/work",
        bound_at=datetime.now(UTC),
        jsonl_path=None,
    )
    binding_store.save_binding(binding)

    result = await resolver.resolve("sess-ext")

    assert result.ownership_state == "bound"
    assert result.origin == SessionOrigin.EXTERNAL
    assert result.owner_user_id == 99
    assert result.binding_id == binding.binding_id


@pytest.mark.asyncio
async def test_resolve_unbound(resolver: SessionOwnershipResolver, session_service: FakeSessionService) -> None:
    """Session with no ownership is unbound."""
    session_service.contexts = []

    result = await resolver.resolve("sess-unknown")

    assert result.ownership_state == "unbound"
    assert result.origin == SessionOrigin.EXTERNAL
    assert result.owner_user_id is None


@pytest.mark.asyncio
async def test_tmux_priority_over_binding(
    resolver: SessionOwnershipResolver,
    session_service: FakeSessionService,
    binding_store: ExternalBindingStore,
) -> None:
    """Tmux ownership takes priority over external binding."""
    session_service.contexts = [
        _make_context(user_id=10, claude_session_id="sess-both", terminal_id="term-x"),
    ]
    binding_store.save_binding(
        ExternalBinding(
            session_id="sess-both",
            user_id=20,
            cwd="/tmp",
            bound_at=datetime.now(UTC),
            jsonl_path=None,
        )
    )

    result = await resolver.resolve("sess-both")

    assert result.ownership_state == "owned"
    assert result.origin == SessionOrigin.TMUX
    assert result.owner_user_id == 10


@pytest.mark.asyncio
async def test_no_workdir_matching_without_terminal_id(
    resolver: SessionOwnershipResolver,
    session_service: FakeSessionService,
) -> None:
    """Session without terminal_id is NOT matched even if claude_session_id matches.

    This ensures workdir-based matching is never used for external sessions.
    A context without terminal_id means it wasn't launched via tmux.
    """
    session_service.contexts = [
        _make_context(
            user_id=5,
            claude_session_id="sess-no-term",
            terminal_id=None,
            workdir="/same/workdir",
        ),
    ]

    result = await resolver.resolve("sess-no-term")

    assert result.ownership_state == "unbound"
    assert result.owner_user_id is None


@pytest.mark.asyncio
async def test_is_tmux_owned_true(resolver: SessionOwnershipResolver, session_service: FakeSessionService) -> None:
    session_service.contexts = [
        _make_context(user_id=1, claude_session_id="sess-t", terminal_id="term-1"),
    ]

    assert await resolver.is_tmux_owned("sess-t") is True


@pytest.mark.asyncio
async def test_is_tmux_owned_false_no_terminal(resolver: SessionOwnershipResolver, session_service: FakeSessionService) -> None:
    session_service.contexts = [
        _make_context(user_id=1, claude_session_id="sess-t", terminal_id=None),
    ]

    assert await resolver.is_tmux_owned("sess-t") is False


@pytest.mark.asyncio
async def test_is_tmux_owned_false_no_match(resolver: SessionOwnershipResolver, session_service: FakeSessionService) -> None:
    session_service.contexts = [
        _make_context(user_id=1, claude_session_id="other-sess", terminal_id="term-1"),
    ]

    assert await resolver.is_tmux_owned("sess-t") is False
