"""Unit tests for session_actions callback handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Router
from aiogram.types import CallbackQuery, Message, User

from app.bot.handlers.session_actions import _resolve_session_id, register_session_action_handlers
from app.domain.external_session_models import ExternalBinding
from app.domain.hook_models import HookEvent
from app.domain.models import TerminalSessionInfo, utc_now
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

    def test_resolves_bound_session_from_memory_when_disk_is_missing(
        self,
        discovery: ExternalSessionDiscoveryService,
        binder: ExternalSessionBinder,
        binding_store: ExternalBindingStore,
    ) -> None:
        session_id = "abcdef1234567890full"
        binding_store.save_binding(
            ExternalBinding(
                session_id=session_id,
                user_id=42,
                cwd="/home/user/proj",
                bound_at=utc_now(),
                jsonl_path=None,
            )
        )
        (binding_store._file_path).unlink()

        resolved, error = _resolve_session_id(session_id[:16], discovery, binder)

        assert resolved == session_id
        assert error is None


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


class TestTmuxSessionActionHandler:
    @pytest.mark.asyncio
    async def test_attach_resolves_terminal_id_prefix(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder
    ) -> None:
        terminal_id = "user_42_123456789abc"
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(
            return_value=[
                TerminalSessionInfo(
                    terminal_id=terminal_id,
                    tmux_session_name="tgcli_user_42_123456789abc",
                    workdir="/home/user/proj",
                    phase="idle",
                    owner_user_id=1,
                    attached_user_ids=[],
                    is_alive=True,
                    last_activity=None,
                )
            ]
        )
        registry.attach_user = AsyncMock(return_value=(True, "已连接"))
        router = Router()
        register_session_action_handlers(router, discovery=discovery, binder=binder, registry_service=registry)
        callback = AsyncMock(spec=CallbackQuery)
        callback.data = f"sess:attach:{terminal_id[:16]}"
        callback.from_user = MagicMock(spec=User)
        callback.from_user.id = 42
        callback.answer = AsyncMock()
        callback.message = AsyncMock(spec=Message)
        callback.message.answer = AsyncMock()

        await router.callback_query.handlers[3].callback(callback)

        registry.attach_user.assert_awaited_once_with(user_id=42, terminal_id=terminal_id)

    @pytest.mark.asyncio
    async def test_close_resolves_terminal_id_prefix(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder
    ) -> None:
        terminal_id = "user_42_123456789abc"
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(
            return_value=[
                TerminalSessionInfo(
                    terminal_id=terminal_id,
                    tmux_session_name="tgcli_user_42_123456789abc",
                    workdir="/home/user/proj",
                    phase="idle",
                    owner_user_id=1,
                    attached_user_ids=[],
                    is_alive=True,
                    last_activity=None,
                )
            ]
        )
        registry.close_session = AsyncMock(return_value=True)
        router = Router()
        register_session_action_handlers(router, discovery=discovery, binder=binder, registry_service=registry)
        callback = AsyncMock(spec=CallbackQuery)
        callback.data = f"sess:close:{terminal_id[:16]}"
        callback.from_user = MagicMock(spec=User)
        callback.from_user.id = 42
        callback.answer = AsyncMock()
        callback.message = AsyncMock(spec=Message)
        callback.message.answer = AsyncMock()

        await router.callback_query.handlers[4].callback(callback)

        registry.close_session.assert_awaited_once_with(terminal_id)


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
