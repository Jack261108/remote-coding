from __future__ import annotations

import hashlib
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
from app.services.session_id_resolver import _resolve_session_id, resolve_and_bind, unique_prefixes


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


def test_resolve_session_id_accepts_hash_token_for_long_common_prefix(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    session_ids = ["x" * 52 + "a", "x" * 52 + "b"]
    for session_id in session_ids:
        _record_unbound(discovery, session_id)
    token = unique_prefixes(session_ids)[session_ids[0]]
    assert token.startswith("~")

    resolved, error = _resolve_session_id(token, discovery, binder)

    assert resolved == session_ids[0]
    assert error is None


def test_unique_prefixes_hashes_dot_ending_session_token() -> None:
    tokens = unique_prefixes(["abc."])

    assert tokens["abc."].startswith("~")


def test_unique_prefixes_avoids_legacy_h_dot_hash_token_shape() -> None:
    token_shaped_session_id = "h." + "a" * 16

    tokens = unique_prefixes([token_shaped_session_id], min_length=len(token_shaped_session_id))

    assert tokens[token_shaped_session_id].startswith("~")


def test_resolve_session_id_accepts_tilde_prefixed_session_token(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    session_id = "~abcdefghijklmnop"
    _record_unbound(discovery, session_id)
    token = unique_prefixes([session_id])[session_id]

    resolved, error = _resolve_session_id(token, discovery, binder)

    assert resolved == session_id
    assert error is None


def test_unique_prefixes_avoids_tilde_hash_token_shape(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    session_id = "~" + "a" * 16
    _record_unbound(discovery, session_id)
    token = unique_prefixes([session_id], min_length=len(session_id))[session_id]

    assert token != session_id
    resolved, error = _resolve_session_id(token, discovery, binder)

    assert resolved == session_id
    assert error is None


def test_resolve_session_id_accepts_exact_token_without_trailing_dot_collision(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    session_ids = ["abc", "abc.", "abcdef"]
    for session_id in session_ids:
        _record_unbound(discovery, session_id)
    tokens = unique_prefixes(session_ids)
    assert tokens["abc"] != "abc."
    assert len(set(tokens.values())) == len(tokens)

    resolved_short, short_error = _resolve_session_id(tokens["abc"], discovery, binder)
    resolved_dot, dot_error = _resolve_session_id(tokens["abc."], discovery, binder)

    assert resolved_short == "abc"
    assert short_error is None
    assert resolved_dot == "abc."
    assert dot_error is None


def test_resolve_session_id_treats_h_dot_prefix_as_normal_session_id(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    session_id = "h.abcdefghijklmnop"
    _record_unbound(discovery, session_id)
    token = unique_prefixes([session_id])[session_id]
    assert token.startswith("h.")

    resolved, error = _resolve_session_id(token, discovery, binder)

    assert resolved == session_id
    assert error is None


@pytest.mark.asyncio
async def test_resolve_and_bind_hash_token_for_unavailable_session_reports_unavailable(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    session_ids = ["y" * 52 + "a", "y" * 52 + "b"]
    token = unique_prefixes(session_ids)[session_ids[0]]
    assert token.startswith("~")
    discovery.mark_session_unavailable(session_ids[0])
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="should not bind"))

    result = await resolve_and_bind(token, user_id=42, discovery=discovery, binder=binder)

    assert result.success is False
    assert result.message == "Session is no longer available"
    binder.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_and_bind_legacy_trailing_dot_live_token_binds_original_session(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    _record_unbound(discovery, "abc")
    _record_unbound(discovery, "abc.")
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="bound"))

    result = await resolve_and_bind("abc.", user_id=42, discovery=discovery, binder=binder)

    assert result.success is True
    assert result.session_id == "abc"
    binder.bind.assert_awaited_once_with(user_id=42, session_id="abc")


@pytest.mark.asyncio
async def test_resolve_and_bind_legacy_h_dot_hash_live_token_binds_original_session(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    old_session_id = "legacy-live-long-session-" + "x" * 60
    old_token = f"h.{hashlib.sha1(old_session_id.encode()).hexdigest()[:16]}"
    _record_unbound(discovery, old_session_id)
    _record_unbound(discovery, old_token)
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="bound"))

    result = await resolve_and_bind(old_token, user_id=42, discovery=discovery, binder=binder)

    assert result.success is True
    assert result.session_id == old_session_id
    binder.bind.assert_awaited_once_with(user_id=42, session_id=old_session_id)


@pytest.mark.asyncio
async def test_resolve_and_bind_legacy_trailing_dot_unavailable_token_does_not_bind_live_session(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    discovery.mark_session_unavailable("abc")
    _record_unbound(discovery, "abc.")
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="should not bind"))

    result = await resolve_and_bind("abc.", user_id=42, discovery=discovery, binder=binder)

    assert result.success is False
    assert result.message == "Session is no longer available"
    binder.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_and_bind_legacy_h_dot_hash_unavailable_token_does_not_bind_live_session(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    old_session_id = "legacy-long-session-" + "x" * 60
    old_token = f"h.{hashlib.sha1(old_session_id.encode()).hexdigest()[:16]}"
    discovery.mark_session_unavailable(old_session_id)
    _record_unbound(discovery, old_token)
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="should not bind"))

    result = await resolve_and_bind(old_token, user_id=42, discovery=discovery, binder=binder)

    assert result.success is False
    assert result.message == "Session is no longer available"
    binder.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_and_bind_unavailable_prefix_does_not_bind_new_exact_live_session(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> None:
    old_session_id = "abcdefghijklmnop-old"
    live_session_id = "abcdefghijklmnop"
    discovery.mark_session_unavailable(old_session_id)
    _record_unbound(discovery, live_session_id)
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="should not bind"))

    result = await resolve_and_bind(live_session_id, user_id=42, discovery=discovery, binder=binder)

    assert result.success is False
    assert result.message == "Session is no longer available"
    binder.bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_and_bind_stale_collision_does_not_bind_live_session_from_old_prefix(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_id = "shared-prefix-old"
    live_id = "shared-prefix-live"
    _record_unbound(discovery, stale_id)
    _record_unbound(discovery, live_id)
    monkeypatch.setattr(discovery, "is_session_stale", lambda session_id: session_id == stale_id)
    binder.bind = AsyncMock(return_value=SimpleNamespace(success=True, conversation_available=False, message="should not bind"))

    result = await resolve_and_bind("shared-prefix-", user_id=42, discovery=discovery, binder=binder)

    assert result.success is False
    assert result.message == "Session is no longer available"
    binder.bind.assert_not_awaited()


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
async def test_resolve_and_bind_accepts_live_unique_prefix_despite_stale_unbound(
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_id = "shared-prefix-0001-stale"
    live_id = "shared-prefix-0001-live"
    _record_unbound(discovery, stale_id)
    _record_unbound(discovery, live_id)
    monkeypatch.setattr(discovery, "is_session_stale", lambda session_id: session_id == stale_id)
    token = unique_prefixes([stale_id, live_id])[live_id]

    result = await resolve_and_bind(token, user_id=42, discovery=discovery, binder=binder)

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
