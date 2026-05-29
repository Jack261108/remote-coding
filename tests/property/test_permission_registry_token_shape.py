from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.permission_callback_registry import (
    AuthorizationMode,
    PermissionAction,
    PermissionCallbackRegistry,
    SessionOrigin,
)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8}$")
BASE_SECONDS = 4_000.0


safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_-"),
    min_size=1,
    max_size=40,
)


@settings(max_examples=50, deadline=None)
@given(tool_use_id=safe_text, session_id=safe_text, origin=st.sampled_from(list(SessionOrigin)))
def test_register_token_returns_eight_url_safe_characters(tool_use_id: str, session_id: str, origin: SessionOrigin) -> None:
    async def run_scenario() -> str:
        registry = PermissionCallbackRegistry(ttl_sec=60, clock=lambda: BASE_SECONDS)
        return await registry.register_token(
            tool_use_id=tool_use_id,
            session_id=session_id,
            origin=origin,
            authorization_mode=AuthorizationMode.ALL_USERS,
            authorized_user_ids=frozenset(),
        )

    token = asyncio.run(run_scenario())

    assert TOKEN_RE.fullmatch(token)


@settings(max_examples=100, deadline=None)
@given(token=st.from_regex(TOKEN_RE, fullmatch=True), action=st.sampled_from(list(PermissionAction)))
def test_permission_callback_data_fits_telegram_limit(token: str, action: PermissionAction) -> None:
    assert len(f"perm:{token}:{action}".encode("utf-8")) <= 64


def test_default_register_token_uses_current_wall_clock_timestamps() -> None:
    async def run_scenario() -> None:
        before = datetime.now(timezone.utc) - timedelta(minutes=1)
        registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok00001")
        token = await registry.register_token(
            tool_use_id="tool-1",
            session_id="session-1",
            origin=SessionOrigin.EXTERNAL_UNBOUND,
            authorization_mode=AuthorizationMode.ALL_USERS,
            authorized_user_ids=frozenset(),
        )
        after = datetime.now(timezone.utc) + timedelta(minutes=1)
        record = registry._records[token]

        assert before <= record.created_at <= after
        assert record.expires_at == record.created_at + timedelta(seconds=60)
        assert record.created_at.year >= 2020

    asyncio.run(run_scenario())


def test_register_token_raises_after_sixteen_token_collisions() -> None:
    calls = 0

    def token_factory() -> str:
        nonlocal calls
        calls += 1
        return "same0001"

    # Build the collision state explicitly so the failing call spends all 16 attempts on the colliding token.
    async def run_collision() -> None:
        registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=token_factory, clock=lambda: BASE_SECONDS)
        first_token = await registry.register_token(
            tool_use_id="tool-1",
            session_id="session-1",
            origin=SessionOrigin.EXTERNAL_UNBOUND,
            authorization_mode=AuthorizationMode.ALL_USERS,
            authorized_user_ids=frozenset(),
        )
        assert first_token == "same0001"
        with pytest.raises(RuntimeError, match="failed to generate unique permission callback token"):
            await registry.register_token(
                tool_use_id="tool-2",
                session_id="session-1",
                origin=SessionOrigin.EXTERNAL_UNBOUND,
                authorization_mode=AuthorizationMode.ALL_USERS,
                authorized_user_ids=frozenset(),
            )

    asyncio.run(run_collision())

    assert calls == 17


def test_register_token_collision_failure_leaves_replaced_record_unchanged() -> None:
    calls = 0

    def token_factory() -> str:
        nonlocal calls
        calls += 1
        return "same0001"

    async def run_collision() -> None:
        registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=token_factory, clock=lambda: BASE_SECONDS)
        first_token = await registry.register_token(
            tool_use_id="tool-1",
            session_id="session-1",
            origin=SessionOrigin.EXTERNAL_UNBOUND,
            authorization_mode=AuthorizationMode.ALL_USERS,
            authorized_user_ids=frozenset(),
        )
        before_records = repr(registry._records)
        before_deadlines = dict(getattr(registry, "_ttl_deadlines", {}))
        before_index = dict(registry._compound_index)

        with pytest.raises(RuntimeError, match="failed to generate unique permission callback token"):
            await registry.register_token(
                tool_use_id="tool-1",
                session_id="session-1",
                origin=SessionOrigin.EXTERNAL_UNBOUND,
                authorization_mode=AuthorizationMode.ALL_USERS,
                authorized_user_ids=frozenset(),
            )

        assert first_token == "same0001"
        assert repr(registry._records) == before_records
        assert getattr(registry, "_ttl_deadlines", {}) == before_deadlines
        assert registry._compound_index == before_index

    asyncio.run(run_collision())

    assert calls == 17


def test_register_token_factory_exception_leaves_replaced_record_unchanged() -> None:
    calls = 0

    def token_factory() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "old00001"
        raise RuntimeError("token factory exploded")

    async def run_failure() -> None:
        registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=token_factory, clock=lambda: BASE_SECONDS)
        await registry.register_token(
            tool_use_id="tool-1",
            session_id="session-1",
            origin=SessionOrigin.EXTERNAL_UNBOUND,
            authorization_mode=AuthorizationMode.ALL_USERS,
            authorized_user_ids=frozenset(),
        )
        before_records = repr(registry._records)
        before_deadlines = dict(getattr(registry, "_ttl_deadlines", {}))
        before_index = dict(registry._compound_index)

        with pytest.raises(RuntimeError, match="token factory exploded"):
            await registry.register_token(
                tool_use_id="tool-1",
                session_id="session-1",
                origin=SessionOrigin.EXTERNAL_UNBOUND,
                authorization_mode=AuthorizationMode.ALL_USERS,
                authorized_user_ids=frozenset(),
            )

        assert repr(registry._records) == before_records
        assert getattr(registry, "_ttl_deadlines", {}) == before_deadlines
        assert registry._compound_index == before_index

    asyncio.run(run_failure())

    assert calls == 2
