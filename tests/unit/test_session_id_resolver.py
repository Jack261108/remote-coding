from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.external_session_models import ExternalBinding
from app.domain.hook_models import HookEvent
from app.domain.models import utc_now
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.session_id_resolver import resolve_and_bind


@pytest.fixture
def discovery() -> ExternalSessionDiscoveryService:
    return ExternalSessionDiscoveryService()


@pytest.fixture
def binding_store(tmp_path: Path) -> ExternalBindingStore:
    return ExternalBindingStore(data_dir=tmp_path)


@pytest.fixture
def binder(discovery: ExternalSessionDiscoveryService, binding_store: ExternalBindingStore, tmp_path: Path) -> ExternalSessionBinder:
    return ExternalSessionBinder(
        discovery=discovery,
        binding_store=binding_store,
        projects_dir=tmp_path / "projects",
    )


def _record_unbound(discovery: ExternalSessionDiscoveryService, session_id: str, *, pid: int | None = None) -> None:
    discovery.record_event(
        HookEvent(
            session_id=session_id,
            cwd="/home/user/project",
            event="PreToolUse",
            status="running",
            pid=pid,
        )
    )


@pytest.mark.asyncio
async def test_resolve_and_bind_rejects_stale_unbound_without_calling_binder_bind(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "stale-unbound-session-0001"
    _record_unbound(discovery, session_id)
    monkeypatch.setattr(discovery, "is_session_stale", lambda _: True)
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="should not bind"))

    result = await resolve_and_bind(session_id[:16], user_id=42, discovery=discovery, binder=binder)

    assert result.success is False
    assert result.message == "Session is no longer available"
    binder.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_and_bind_rejects_dead_pid_unbound_without_calling_binder_bind(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    session_id = "dead-pid-unbound-session-0001"
    _record_unbound(discovery, session_id, pid=12345)
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="should not bind"))

    with patch("app.services.session_id_resolver.process_is_alive", return_value=False):
        result = await resolve_and_bind(session_id[:16], user_id=42, discovery=discovery, binder=binder)

    assert result.success is False
    assert result.message == "Session is no longer available"
    binder.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_and_bind_bad_pid_unbound_does_not_crash_or_mark_unavailable(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    session_id = "bad-pid-unbound-session-0001"
    _record_unbound(discovery, session_id, pid=2**100)

    with patch("app.services.session_id_resolver.process_is_alive", side_effect=OverflowError("bad pid")):
        result = await resolve_and_bind(session_id[:16], user_id=42, discovery=discovery, binder=binder)

    assert result.success is True
    assert result.session_id == session_id


@pytest.mark.asyncio
async def test_resolve_and_bind_bound_session_prefix_is_not_available_to_bind(
    binding_store: ExternalBindingStore,
    binder: ExternalSessionBinder,
) -> None:
    session_id = "already-bound-session-0001"
    binding_store.save_binding(
        ExternalBinding(
            session_id=session_id,
            user_id=42,
            cwd="/home/user/project",
            bound_at=utc_now(),
            jsonl_path=None,
        )
    )
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="should not bind"))

    result = await resolve_and_bind(session_id[:16], user_id=42, discovery=binder._discovery, binder=binder)

    assert result.success is False
    assert result.message == "Session is not available to bind"
    binder.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_and_bind_active_unbound_preserves_conversation_status_message(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    session_id = "active-unbound-session-0001"
    _record_unbound(discovery, session_id)

    result = await resolve_and_bind(session_id[:16], user_id=42, discovery=discovery, binder=binder)

    assert result.success is True
    assert result.session_id == session_id
    assert result.message == "⏳ waiting for JSONL"
    assert result.conversation_available is False


@pytest.mark.asyncio
async def test_resolve_and_bind_ignores_stale_unbound_collision_for_live_unbound(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_id = "shared-prefix-0001-stale"
    live_id = "shared-prefix-0001-live"
    _record_unbound(discovery, stale_id)
    _record_unbound(discovery, live_id)
    monkeypatch.setattr(discovery, "is_session_stale", lambda session_id: session_id == stale_id)

    result = await resolve_and_bind(live_id[:16], user_id=42, discovery=discovery, binder=binder)

    assert result.success is True
    assert result.session_id == live_id


@pytest.mark.asyncio
async def test_resolve_and_bind_exact_stale_unbound_does_not_bind_live_prefix_extension(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_id = "abc"
    live_id = "abcdef"
    _record_unbound(discovery, stale_id)
    _record_unbound(discovery, live_id)
    monkeypatch.setattr(discovery, "is_session_stale", lambda session_id: session_id == stale_id)
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="should not bind"))

    result = await resolve_and_bind(stale_id, user_id=42, discovery=discovery, binder=binder)

    assert result.success is False
    assert result.message == "Session is no longer available"
    binder.bind.assert_not_awaited()
