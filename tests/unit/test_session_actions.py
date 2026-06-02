"""Unit tests for session_actions callback handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Router
from aiogram.types import CallbackQuery, Message, User

from app.bot.handlers.session_actions import _resolve_session_id, register_session_action_handlers
from app.domain.hook_models import HookEvent
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService


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


class TestResolveSessionId:
    def test_resolves_from_discovery(self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder) -> None:
        event = HookEvent(session_id="abcdef1234567890full", cwd="/tmp", event="PreToolUse", status="running")
        discovery.record_event(event)

        resolved, error = _resolve_session_id("abcdef1234567890", discovery, binder)
        assert resolved == "abcdef1234567890full"
        assert error is None

    def test_not_found(self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder) -> None:
        resolved, error = _resolve_session_id("nonexistent", discovery, binder)
        assert resolved is None
        assert error == "Session not found"


class TestSessionSelectHandler:
    @pytest.mark.asyncio
    async def test_select_unbound_session_shows_bind_button(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder
    ) -> None:
        session_id = "abcdef1234567890full"
        event = HookEvent(session_id=session_id, cwd="/home/user/proj", event="PreToolUse", status="running")
        discovery.record_event(event)

        router = Router()
        register_session_action_handlers(router, discovery=discovery, binder=binder)

        # Simulate callback
        callback = AsyncMock(spec=CallbackQuery)
        callback.data = f"sess:select:{session_id[:16]}"
        callback.from_user = MagicMock(spec=User)
        callback.from_user.id = 42
        callback.message = AsyncMock(spec=Message)

        resolved, error = _resolve_session_id(session_id[:16], discovery, binder)
        assert resolved == session_id
        assert error is None

        # Verify the session is unbound (no binding exists)
        binding = binder._binding_store.get_binding(session_id)
        assert binding is None  # unbound, so "绑定" button should show

    @pytest.mark.asyncio
    async def test_select_bound_session_shows_unbind_button(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder
    ) -> None:
        session_id = "abcdef1234567890full"
        user_id = 42
        event = HookEvent(session_id=session_id, cwd="/home/user/proj", event="PreToolUse", status="running")
        discovery.record_event(event)

        # Bind the session
        result = await binder.bind(user_id=user_id, session_id=session_id)
        assert result.success

        # Now check binding state
        binding = binder._binding_store.get_binding(session_id)
        assert binding is not None
        assert binding.user_id == user_id  # bound to user, so "取消绑定" button should show


class TestSessionBindHandler:
    @pytest.mark.asyncio
    async def test_bind_delegates_to_binder(self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder) -> None:
        session_id = "abcdef1234567890full"
        event = HookEvent(session_id=session_id, cwd="/home/user/proj", event="PreToolUse", status="running")
        discovery.record_event(event)

        result = await binder.bind(user_id=42, session_id=session_id)
        assert result.success is True
        assert result.session_id == session_id

    @pytest.mark.asyncio
    async def test_bind_already_bound_fails(self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder) -> None:
        session_id = "abcdef1234567890full"
        event = HookEvent(session_id=session_id, cwd="/home/user/proj", event="PreToolUse", status="running")
        discovery.record_event(event)

        await binder.bind(user_id=42, session_id=session_id)
        # Session removed from discovery after bind, so second bind won't find it
        result = await binder.bind(user_id=99, session_id=session_id)
        assert result.success is False


class TestSessionUnbindHandler:
    @pytest.mark.asyncio
    async def test_unbind_delegates_to_binder(self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder) -> None:
        session_id = "abcdef1234567890full"
        event = HookEvent(session_id=session_id, cwd="/home/user/proj", event="PreToolUse", status="running")
        discovery.record_event(event)

        await binder.bind(user_id=42, session_id=session_id)
        result = await binder.unbind(user_id=42, session_id=session_id)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_unbind_wrong_user_fails(self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder) -> None:
        session_id = "abcdef1234567890full"
        event = HookEvent(session_id=session_id, cwd="/home/user/proj", event="PreToolUse", status="running")
        discovery.record_event(event)

        await binder.bind(user_id=42, session_id=session_id)
        result = await binder.unbind(user_id=99, session_id=session_id)
        assert result.success is False
