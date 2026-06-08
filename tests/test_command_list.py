from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Router

from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.adapters.storage.file_session_store import FileSessionStore
from app.bot.handlers.command_list import register_list_handler
from app.domain.external_session_models import ExternalBinding
from app.domain.hook_models import HookEvent
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.session_lookup_service import SessionLookupService
from app.services.session_registry import SessionRegistryService
from app.services.session_service import SessionService
from app.services.session_state_cache import SessionStateCache
from app.services.session_state_repository import SessionStateRepository


class FakeTmuxRunner:
    def __init__(self) -> None:
        self._alive_sessions: set[str] = set()

    def build_session_name(self, terminal_key: str) -> str:
        return f"tgcli_{terminal_key}"[:64]

    async def session_exists(self, session_name: str) -> bool:
        return session_name in self._alive_sessions

    async def list_managed_sessions(self) -> list[str]:
        return sorted(s for s in self._alive_sessions if s.startswith("tgcli_"))


def _setup(tmp_path, *, alive_sessions: set[str] | None = None):
    file_store = FileSessionStore(str(tmp_path))
    ctx_store = FileSessionContextStore(file_store)
    session_service = SessionService(store=ctx_store)
    repository = SessionStateRepository(file_store)
    cache = SessionStateCache(repository)
    lookup = SessionLookupService(cache, repository)
    tmux = FakeTmuxRunner()
    if alive_sessions:
        tmux._alive_sessions = alive_sessions
    registry = SessionRegistryService(
        session_service=session_service,
        lookup=lookup,
        tmux_runner=tmux,
        repository=repository,
    )
    router = Router()
    register_list_handler(router, registry_service=registry)
    return registry, session_service, cache


@pytest.mark.asyncio
async def test_list_shows_no_sessions(tmp_path) -> None:
    registry, _, _ = _setup(tmp_path)
    sessions = await registry.list_active_sessions()
    assert sessions == []


@pytest.mark.asyncio
async def test_list_shows_active_session(tmp_path) -> None:
    registry, session_service, cache = _setup(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    ctx = await session_service.get(1)
    ctx.terminal_id = "user_1_abc123"
    await session_service._store.save(ctx)
    state = cache.get_or_create(
        session_id="s1",
        provider="claude_code",
        workdir="/proj",
        terminal_id="user_1_abc123",
        user_id=1,
    )
    activity_at = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    state.last_activity = activity_at
    cache.put(state)

    sessions = await registry.list_active_sessions()
    assert len(sessions) == 1
    assert sessions[0].terminal_id == "user_1_abc123"
    assert sessions[0].workdir == "/proj"
    assert sessions[0].owner_user_id == 1
    assert sessions[0].last_activity == activity_at


def _message(user_id: int = 42) -> MagicMock:
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.answer = AsyncMock()
    return message


def _callback_data_from_answer(message: MagicMock) -> list[str]:
    keyboard = message.answer.call_args.kwargs.get("reply_markup")
    if keyboard is None:
        return []
    return [button.callback_data or "" for row in keyboard.inline_keyboard for button in row]


def _save_external_binding(
    store: ExternalBindingStore,
    *,
    session_id: str,
    user_id: int,
    title: str,
    activity_at: datetime,
    pid: int | None = None,
) -> None:
    store.save_binding(
        ExternalBinding(
            session_id=session_id,
            user_id=user_id,
            cwd="/Users/jack/project/remote-coding",
            bound_at=activity_at - timedelta(hours=1),
            jsonl_path=None,
            title=title,
            last_activity_at_init=activity_at,
            pid=pid,
        )
    )


@pytest.mark.asyncio
async def test_command_list_renders_recent_bound_summary(tmp_path: Path) -> None:
    store = ExternalBindingStore(data_dir=tmp_path)
    user_id = 42
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    _save_external_binding(store, session_id="sess-newest-0001", user_id=user_id, title="Newest", activity_at=now)
    _save_external_binding(store, session_id="sess-second-0002", user_id=user_id, title="Second", activity_at=now - timedelta(minutes=2))
    _save_external_binding(store, session_id="sess-third-0003", user_id=user_id, title="Third", activity_at=now - timedelta(minutes=3))
    _save_external_binding(store, session_id="sess-hidden-0004", user_id=user_id, title="Hidden", activity_at=now - timedelta(minutes=4))

    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    binder = ExternalSessionBinder(
        discovery=ExternalSessionDiscoveryService(),
        binding_store=store,
        projects_dir=tmp_path / "projects",
    )
    router = Router()
    register_list_handler(router, registry_service=registry, external_binder=binder)
    handler = router.message.handlers[-1].callback
    message = _message(user_id)

    await handler(message)

    text = message.answer.call_args.args[0]
    assert "📋 <b>会话</b>" in text
    assert "🚀 <b>最近可继续</b>" in text
    assert "1. 🔗 Newest" in text
    assert "2. 🔗 Second" in text
    assert "3. 🔗 Third" in text
    assert "Hidden" not in text
    assert "还有 1 个旧会话未显示" in text
    assert _callback_data_from_answer(message) == [
        "sess:select:sess-newest-0001",
        "sess:select:sess-second-0002",
        "sess:select:sess-third-0003",
        "sess:list:all",
    ]


@pytest.mark.asyncio
async def test_command_list_includes_cleanup_when_only_invalid_sessions_remain(tmp_path: Path) -> None:
    from unittest.mock import patch

    discovery = ExternalSessionDiscoveryService()
    discovery.record_event(
        HookEvent(
            session_id="dead-unbound-0001",
            cwd="/Users/jack/project/remote-coding",
            event="PreToolUse",
            status="running",
            pid=12345,
        )
    )
    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    router = Router()
    register_list_handler(router, registry_service=registry, external_discovery=discovery)
    handler = router.message.handlers[-1].callback
    message = _message()

    with patch("app.bot.handlers.command_list.process_is_alive", return_value=False):
        await handler(message)

    assert message.answer.call_args.args[0] == "当前无活跃会话。"
    assert _callback_data_from_answer(message) == ["sess:cleanup"]


@pytest.mark.asyncio
async def test_list_all_callback_uses_tmux_terminal_id_prefix(tmp_path: Path) -> None:
    terminal_id = "user_42_123456789abc"
    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(
        return_value=[
            SimpleNamespace(
                terminal_id=terminal_id,
                workdir="/proj",
                phase="idle",
                owner_user_id=42,
                attached_user_ids=[],
                is_alive=True,
                last_activity=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
            )
        ]
    )
    router = Router()
    register_list_handler(router, registry_service=registry)
    callback_handler = router.callback_query.handlers[0].callback

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=42)
    callback.data = "sess:list:all"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    await callback_handler(callback)

    callbacks = [
        button.callback_data or "" for row in callback.message.answer.call_args.kwargs["reply_markup"].inline_keyboard for button in row
    ]
    assert f"sess:attach:{terminal_id[:16]}" in callbacks
    assert f"sess:close:{terminal_id[:16]}" in callbacks


@pytest.mark.asyncio
async def test_list_all_callback_uses_unique_tmux_terminal_id_prefixes_for_same_user(tmp_path: Path) -> None:
    terminal_ids = [
        "user_1234567890_aaaaaaaaaaaa",
        "user_1234567890_bbbbbbbbbbbb",
    ]
    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(
        return_value=[
            SimpleNamespace(
                terminal_id=terminal_id,
                workdir=f"/proj/{index}",
                phase="idle",
                owner_user_id=1234567890,
                attached_user_ids=[],
                is_alive=True,
                last_activity=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
            )
            for index, terminal_id in enumerate(terminal_ids, start=1)
        ]
    )
    router = Router()
    register_list_handler(router, registry_service=registry)
    callback_handler = router.callback_query.handlers[0].callback

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=1234567890)
    callback.data = "sess:list:all"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    await callback_handler(callback)

    callbacks = [
        button.callback_data or "" for row in callback.message.answer.call_args.kwargs["reply_markup"].inline_keyboard for button in row
    ]
    attach_suffixes = [callback.removeprefix("sess:attach:") for callback in callbacks if callback.startswith("sess:attach:")]
    close_suffixes = [callback.removeprefix("sess:close:") for callback in callbacks if callback.startswith("sess:close:")]

    assert len(attach_suffixes) == 2
    assert len(set(attach_suffixes)) == 2
    assert len(close_suffixes) == 2
    assert len(set(close_suffixes)) == 2
    for suffix in attach_suffixes + close_suffixes:
        assert sum(terminal_id.startswith(suffix) for terminal_id in terminal_ids) == 1


@pytest.mark.asyncio
async def test_list_all_callback_renders_full_legacy_list(tmp_path: Path) -> None:
    store = ExternalBindingStore(data_dir=tmp_path)
    user_id = 42
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    _save_external_binding(store, session_id="sess-newest-0001", user_id=user_id, title="Newest", activity_at=now)
    _save_external_binding(store, session_id="sess-second-0002", user_id=user_id, title="Second", activity_at=now - timedelta(minutes=2))
    _save_external_binding(store, session_id="sess-third-0003", user_id=user_id, title="Third", activity_at=now - timedelta(minutes=3))
    _save_external_binding(store, session_id="sess-hidden-0004", user_id=user_id, title="Hidden", activity_at=now - timedelta(minutes=4))

    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    binder = ExternalSessionBinder(
        discovery=ExternalSessionDiscoveryService(),
        binding_store=store,
        projects_dir=tmp_path / "projects",
    )
    router = Router()
    register_list_handler(router, registry_service=registry, external_binder=binder)
    # 找到 sess:list:all callback handler
    callback_handler = None
    for handler in router.callback_query.handlers:
        if hasattr(handler, "filter") and handler.filter is not None:
            # 通过 data 匹配找到正确的 handler
            pass
    # 直接使用第二个 callback handler（list:all 是第一个）
    callback_handler = router.callback_query.handlers[0].callback

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.data = "sess:list:all"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    await callback_handler(callback)

    callback.answer.assert_awaited_once()
    text = callback.message.answer.call_args.args[0]
    assert "📋 <b>活跃会话</b>" in text
    assert "project/remote-coding" in text
    assert "已绑定" in text
    assert text.count("🔗") == 4  # all 4 bound sessions shown
    callbacks = [
        button.callback_data or "" for row in callback.message.answer.call_args.kwargs["reply_markup"].inline_keyboard for button in row
    ]
    assert "sess:select:sess-hidden-0004" in callbacks


@pytest.mark.asyncio
async def test_cleanup_removes_dead_sessions_and_refreshes(tmp_path: Path) -> None:
    from unittest.mock import patch

    # 创建 dead pid binding（需要设置 pid）
    store = ExternalBindingStore(data_dir=tmp_path)
    user_id = 42
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    _save_external_binding(store, session_id="sess-dead-00001", user_id=user_id, title="Dead", activity_at=now, pid=12345)

    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    binder = ExternalSessionBinder(
        discovery=ExternalSessionDiscoveryService(),
        binding_store=store,
        projects_dir=tmp_path / "projects",
    )
    reaper = AsyncMock()
    reaper.remove_with_cleanup = AsyncMock(return_value=True)

    router = Router()
    register_list_handler(
        router,
        registry_service=registry,
        external_binder=binder,
        liveness_enabled=True,
        reaper=reaper,
    )
    # cleanup 是最后一个 callback handler
    callback_handler = router.callback_query.handlers[-1].callback

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.data = "sess:cleanup"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    with patch("app.bot.handlers.command_list.process_is_alive", return_value=False):
        await callback_handler(callback)

    # 验证 reaper 被调用（cleanup + collect_items 刷新时各一次）
    assert reaper.remove_with_cleanup.await_count >= 1
    reaper.remove_with_cleanup.assert_any_await("sess-dead-00001", reason="pid_dead")

    # 验证 toast 提示
    callback.answer.assert_awaited_once()

    # 验证刷新消息（清理后可能无活跃会话）
    text = callback.message.answer.call_args.args[0]
    assert "会话" in text  # 刷新后的摘要或无会话提示


@pytest.mark.asyncio
async def test_command_list_bound_bad_pid_does_not_fail(tmp_path: Path) -> None:
    from unittest.mock import patch

    store = ExternalBindingStore(data_dir=tmp_path)
    user_id = 42
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    _save_external_binding(store, session_id="bound-bad-pid", user_id=user_id, title="BadPid", activity_at=now, pid=2**100)

    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    binder = ExternalSessionBinder(
        discovery=ExternalSessionDiscoveryService(),
        binding_store=store,
        projects_dir=tmp_path / "projects",
    )
    reaper = AsyncMock()
    reaper.remove_with_cleanup = AsyncMock(return_value=True)

    router = Router()
    register_list_handler(
        router,
        registry_service=registry,
        external_binder=binder,
        liveness_enabled=True,
        reaper=reaper,
    )
    handler = router.message.handlers[-1].callback
    message = _message(user_id)

    with patch("app.bot.handlers.command_list.process_is_alive", side_effect=OverflowError("bad pid")):
        await handler(message)

    text = message.answer.call_args.args[0]
    assert "BadPid" in text
    reaper.remove_with_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_bound_bad_pid_does_not_fail_or_reap(tmp_path: Path) -> None:
    from unittest.mock import patch

    store = ExternalBindingStore(data_dir=tmp_path)
    user_id = 42
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    _save_external_binding(store, session_id="bound-bad-pid-cleanup", user_id=user_id, title="BadPid", activity_at=now, pid=2**100)

    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    binder = ExternalSessionBinder(
        discovery=ExternalSessionDiscoveryService(),
        binding_store=store,
        projects_dir=tmp_path / "projects",
    )
    reaper = AsyncMock()
    reaper.remove_with_cleanup = AsyncMock(return_value=True)

    router = Router()
    register_list_handler(
        router,
        registry_service=registry,
        external_binder=binder,
        liveness_enabled=True,
        reaper=reaper,
    )
    callback_handler = router.callback_query.handlers[-1].callback

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.data = "sess:cleanup"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    with patch("app.bot.handlers.command_list.process_is_alive", side_effect=OverflowError("bad pid")):
        await callback_handler(callback)

    reaper.remove_with_cleanup.assert_not_awaited()
    callback.answer.assert_awaited_once_with("已清理 0 个无效会话")
    callback.message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_dead_unbound_session_tombstones_and_runs_pending_cleanup(tmp_path: Path) -> None:
    from unittest.mock import patch

    session_id = "dead-unbound-0001"
    discovery = ExternalSessionDiscoveryService()
    discovery.record_event(
        HookEvent(
            session_id=session_id,
            cwd="/Users/jack/project/remote-coding",
            event="PreToolUse",
            status="running",
            pid=12345,
        )
    )
    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    cleanup_calls: list[str] = []

    async def cleanup_dead_unbound_session(dead_session_id: str) -> None:
        cleanup_calls.append(dead_session_id)

    router = Router()
    register_list_handler(
        router,
        registry_service=registry,
        external_discovery=discovery,
        dead_unbound_cleanup=cleanup_dead_unbound_session,
    )
    callback_handler = router.callback_query.handlers[-1].callback

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=42)
    callback.data = "sess:cleanup"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    with patch("app.services.external_session_discovery.process_is_alive", return_value=False):
        await callback_handler(callback)

    assert cleanup_calls == [session_id]
    assert discovery.get(session_id) is None
    assert discovery.is_session_ended(session_id) is True

    discovery.record_event(
        HookEvent(
            session_id=session_id,
            cwd="/Users/jack/project/remote-coding",
            event="PreToolUse",
            status="running",
            pid=99999,
        )
    )
    assert discovery.get(session_id) is None


@pytest.mark.asyncio
async def test_cleanup_dead_unbound_session_uses_discovery_prune_dead_so_bad_pid_does_not_block(tmp_path: Path) -> None:
    from unittest.mock import patch

    bad_session_id = "bad-pid-unbound"
    dead_session_id = "dead-unbound-0002"
    discovery = ExternalSessionDiscoveryService()
    discovery.record_event(
        HookEvent(
            session_id=bad_session_id,
            cwd="/Users/jack/project/remote-coding",
            event="PreToolUse",
            status="running",
            pid=2**100,
        )
    )
    discovery.record_event(
        HookEvent(
            session_id=dead_session_id,
            cwd="/Users/jack/project/remote-coding",
            event="PreToolUse",
            status="running",
            pid=12345,
        )
    )
    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    cleanup_calls: list[str] = []

    async def cleanup_dead_unbound_session(dead_session_id_arg: str) -> None:
        cleanup_calls.append(dead_session_id_arg)

    router = Router()
    register_list_handler(
        router,
        registry_service=registry,
        external_discovery=discovery,
        dead_unbound_cleanup=cleanup_dead_unbound_session,
    )
    callback_handler = router.callback_query.handlers[-1].callback

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=42)
    callback.data = "sess:cleanup"
    callback.answer = AsyncMock()
    callback.message = None

    def fake_discovery_liveness(pid: int) -> bool:
        if pid == 2**100:
            raise OverflowError("bad pid")
        return False

    with (
        patch("app.bot.handlers.command_list.process_is_alive", side_effect=AssertionError("use discovery prune_dead")),
        patch("app.services.external_session_discovery.process_is_alive", side_effect=fake_discovery_liveness),
    ):
        await callback_handler(callback)

    assert cleanup_calls == [dead_session_id]
    assert discovery.get(bad_session_id) is not None
    assert discovery.get(dead_session_id) is None
    assert discovery.is_session_ended(dead_session_id) is True


@pytest.mark.asyncio
async def test_cleanup_refresh_does_not_fail_when_bad_pid_remains_after_prune_dead(tmp_path: Path) -> None:
    from unittest.mock import patch

    bad_session_id = "bad-pid-unbound-refresh"
    dead_session_id = "dead-unbound-refresh"
    discovery = ExternalSessionDiscoveryService()
    discovery.record_event(
        HookEvent(
            session_id=bad_session_id,
            cwd="/Users/jack/project/remote-coding",
            event="PreToolUse",
            status="running",
            pid=2**100,
        )
    )
    discovery.record_event(
        HookEvent(
            session_id=dead_session_id,
            cwd="/Users/jack/project/remote-coding",
            event="PreToolUse",
            status="running",
            pid=12345,
        )
    )
    registry = AsyncMock()
    registry.list_active_sessions = AsyncMock(return_value=[])
    cleanup_calls: list[str] = []

    async def cleanup_dead_unbound_session(dead_session_id_arg: str) -> None:
        cleanup_calls.append(dead_session_id_arg)

    router = Router()
    register_list_handler(
        router,
        registry_service=registry,
        external_discovery=discovery,
        dead_unbound_cleanup=cleanup_dead_unbound_session,
    )
    callback_handler = router.callback_query.handlers[-1].callback

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=42)
    callback.data = "sess:cleanup"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    def fake_discovery_liveness(pid: int) -> bool:
        if pid == 2**100:
            raise OverflowError("bad pid")
        return False

    def fake_list_liveness(pid: int) -> bool:
        if pid == 2**100:
            raise OverflowError("bad pid")
        return False

    with (
        patch("app.services.external_session_discovery.process_is_alive", side_effect=fake_discovery_liveness),
        patch("app.bot.handlers.command_list.process_is_alive", side_effect=fake_list_liveness),
    ):
        await callback_handler(callback)

    assert cleanup_calls == [dead_session_id]
    callback.answer.assert_awaited_once_with("已清理 1 个无效会话")
    callback.message.answer.assert_awaited_once()
    assert discovery.get(bad_session_id) is not None
    assert discovery.get(dead_session_id) is None
