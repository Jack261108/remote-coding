"""Unit tests for session_actions callback handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Router
from aiogram.types import CallbackQuery, Message, User

from app.bot.handlers.session_actions import _resolve_session_id, _resolve_terminal_id_prefix, register_session_action_handlers
from app.domain.external_session_models import ExternalBinding
from app.domain.hook_models import HookEvent
from app.domain.models import TerminalSessionInfo, utc_now
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.session_id_resolver import unique_prefixes


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


def _tmux_session(terminal_id: str, *, is_alive: bool = True) -> TerminalSessionInfo:
    return TerminalSessionInfo(
        terminal_id=terminal_id,
        tmux_session_name=f"tgcli_{terminal_id}",
        workdir="/home/user/proj",
        phase="idle",
        owner_user_id=1,
        attached_user_ids=[],
        is_alive=is_alive,
        last_activity=None,
    )


class TestUniquePrefixes:
    def test_single_tmux_terminal_id_keeps_workdir_hash_in_prefix(self) -> None:
        prefixes = unique_prefixes(["user_1234567890_aaaaaaaaaaaa"])

        assert prefixes["user_1234567890_aaaaaaaaaaaa"] == "user_1234567890_a"

    def test_short_id_that_is_another_id_prefix_uses_full_id_with_sentinel(self) -> None:
        prefixes = unique_prefixes(["abc", "abcdef"])

        assert prefixes["abc"] == "abc."
        assert prefixes["abcdef"] == "abcdef"

    def test_max_length_caps_unrepresentable_common_prefix(self) -> None:
        first = "x" * 52 + "a"
        second = "x" * 52 + "b"

        prefixes = unique_prefixes([first, second], max_length=52)

        assert len(prefixes[first]) <= 52
        assert len(prefixes[second]) <= 52

    def test_full_id_sentinel_respects_max_length(self) -> None:
        shorter = "x" * 52
        longer = f"{shorter}a"

        prefixes = unique_prefixes([shorter, longer], max_length=52)

        assert len(prefixes[shorter]) <= 52
        assert prefixes[shorter].startswith("h.")


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

    @pytest.mark.asyncio
    async def test_select_active_unbound_callback_still_renders_bind_button(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder
    ) -> None:
        session_id = "active-unbound-session-0001"
        discovery.record_event(HookEvent(session_id=session_id, cwd="/home/user/proj", event="PreToolUse", status="running"))
        router = Router()
        register_session_action_handlers(router, discovery=discovery, binder=binder)
        callback = AsyncMock(spec=CallbackQuery)
        callback.data = f"sess:select:{session_id[:16]}"
        callback.from_user = MagicMock(spec=User)
        callback.from_user.id = 42
        callback.answer = AsyncMock()
        callback.message = AsyncMock(spec=Message)
        callback.message.answer = AsyncMock()

        await router.callback_query.handlers[0].callback(callback)

        callback.answer.assert_awaited_once_with()
        callback.message.answer.assert_awaited_once()
        keyboard = callback.message.answer.call_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        assert callbacks == [f"sess:bind:{session_id[:16]}"]

    @pytest.mark.asyncio
    async def test_select_stale_unbound_callback_does_not_render_bind_button(
        self,
        discovery: ExternalSessionDiscoveryService,
        binder: ExternalSessionBinder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session_id = "stale-unbound-session-0001"
        discovery.record_event(HookEvent(session_id=session_id, cwd="/home/user/proj", event="PreToolUse", status="running"))
        monkeypatch.setattr(discovery, "is_session_stale", lambda _: True)
        router = Router()
        register_session_action_handlers(router, discovery=discovery, binder=binder)
        callback = AsyncMock(spec=CallbackQuery)
        callback.data = f"sess:select:{session_id[:16]}"
        callback.from_user = MagicMock(spec=User)
        callback.from_user.id = 42
        callback.answer = AsyncMock()
        callback.message = AsyncMock(spec=Message)
        callback.message.answer = AsyncMock()

        await router.callback_query.handlers[0].callback(callback)

        callback.answer.assert_awaited_once_with("Session is no longer available")
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_select_dead_pid_unbound_callback_does_not_render_bind_button(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = "dead-pid-unbound-session-0001"
        discovery.record_event(HookEvent(session_id=session_id, cwd="/home/user/proj", event="PreToolUse", status="running", pid=12345))
        monkeypatch.setattr("app.services.session_id_resolver.process_is_alive", lambda _: False)
        router = Router()
        register_session_action_handlers(router, discovery=discovery, binder=binder)
        callback = AsyncMock(spec=CallbackQuery)
        callback.data = f"sess:select:{session_id[:16]}"
        callback.from_user = MagicMock(spec=User)
        callback.from_user.id = 42
        callback.answer = AsyncMock()
        callback.message = AsyncMock(spec=Message)
        callback.message.answer = AsyncMock()

        await router.callback_query.handlers[0].callback(callback)

        callback.answer.assert_awaited_once_with("Session is no longer available")
        callback.message.answer.assert_not_awaited()


class TestTmuxSessionActionHandler:
    @pytest.mark.asyncio
    async def test_resolve_terminal_id_prefix_ignores_dead_sessions(self) -> None:
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(return_value=[_tmux_session("user_1234567890_aaaaaaaaaaaa", is_alive=False)])

        resolved, error = await _resolve_terminal_id_prefix("user_1234567890_", registry)

        assert resolved is None
        assert error == "Session not found"

    @pytest.mark.asyncio
    async def test_resolve_terminal_id_prefix_does_not_resolve_user_wide_legacy_prefix_to_new_session(self) -> None:
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(return_value=[_tmux_session("user_1234567890_bbbbbbbbbbbb")])

        resolved, error = await _resolve_terminal_id_prefix("user_1234567890_", registry)

        assert resolved is None
        assert error == "Session not found"

    @pytest.mark.asyncio
    async def test_resolve_terminal_id_prefix_accepts_full_id_with_sentinel_when_it_is_another_id_prefix(self) -> None:
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(
            return_value=[
                _tmux_session("abc"),
                _tmux_session("abcdef"),
            ]
        )

        resolved, error = await _resolve_terminal_id_prefix("abc.", registry)

        assert resolved == "abc"
        assert error is None

    @pytest.mark.asyncio
    async def test_resolve_terminal_id_prefix_accepts_compact_hash_token_for_long_common_prefix(self) -> None:
        terminal_ids = ["x" * 52 + "a", "x" * 52 + "b"]
        token = unique_prefixes(terminal_ids, max_length=52)[terminal_ids[0]]
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(return_value=[_tmux_session(terminal_id) for terminal_id in terminal_ids])

        resolved, error = await _resolve_terminal_id_prefix(token, registry)

        assert resolved == terminal_ids[0]
        assert error is None

    @pytest.mark.asyncio
    async def test_resolve_terminal_id_prefix_reports_ambiguous_live_matches(self) -> None:
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(
            return_value=[
                _tmux_session("terminal-aaaaaaaaaaaa"),
                _tmux_session("terminal-abbbbbbbbbbb"),
            ]
        )

        resolved, error = await _resolve_terminal_id_prefix("terminal-a", registry)

        assert resolved is None
        assert error == "Ambiguous prefix, 2 matches. Be more specific."

    @pytest.mark.asyncio
    async def test_attach_resolves_terminal_id_prefix(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder
    ) -> None:
        terminal_id = "user_42_123456789abc"
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(return_value=[_tmux_session(terminal_id)])
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
    async def test_attach_stale_dead_session_does_not_call_registry_attach(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder
    ) -> None:
        terminal_id = "user_1234567890_aaaaaaaaaaaa"
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(return_value=[_tmux_session(terminal_id, is_alive=False)])
        registry.attach_user = AsyncMock(return_value=(True, "should not attach"))
        router = Router()
        register_session_action_handlers(router, discovery=discovery, binder=binder, registry_service=registry)
        callback = AsyncMock(spec=CallbackQuery)
        callback.data = "sess:attach:user_1234567890_"
        callback.from_user = MagicMock(spec=User)
        callback.from_user.id = 42
        callback.answer = AsyncMock()
        callback.message = AsyncMock(spec=Message)
        callback.message.answer = AsyncMock()

        await router.callback_query.handlers[3].callback(callback)

        callback.answer.assert_awaited_once_with("Session not found")
        registry.attach_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_resolves_terminal_id_prefix(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder
    ) -> None:
        terminal_id = "user_42_123456789abc"
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(return_value=[_tmux_session(terminal_id)])
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

    @pytest.mark.asyncio
    async def test_close_stale_dead_session_does_not_call_registry_close(
        self, discovery: ExternalSessionDiscoveryService, binder: ExternalSessionBinder
    ) -> None:
        terminal_id = "user_1234567890_aaaaaaaaaaaa"
        registry = AsyncMock()
        registry.list_active_sessions = AsyncMock(return_value=[_tmux_session(terminal_id, is_alive=False)])
        registry.close_session = AsyncMock(return_value=True)
        router = Router()
        register_session_action_handlers(router, discovery=discovery, binder=binder, registry_service=registry)
        callback = AsyncMock(spec=CallbackQuery)
        callback.data = "sess:close:user_1234567890_"
        callback.from_user = MagicMock(spec=User)
        callback.from_user.id = 42
        callback.answer = AsyncMock()
        callback.message = AsyncMock(spec=Message)
        callback.message.answer = AsyncMock()

        await router.callback_query.handlers[4].callback(callback)

        callback.answer.assert_awaited_once_with("Session not found")
        registry.close_session.assert_not_awaited()


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
