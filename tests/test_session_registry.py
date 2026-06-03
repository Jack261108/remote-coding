from __future__ import annotations

from datetime import timedelta

import pytest

from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.adapters.storage.file_session_store import FileSessionStore
from app.domain.models import utc_now
from app.domain.session_models import SessionState
from app.services.session_lookup_service import SessionLookupService
from app.services.session_registry import SessionRegistryService
from app.services.session_service import SessionService
from app.services.session_state_cache import SessionStateCache
from app.services.session_state_repository import SessionStateRepository


class FakeTmuxRunner:
    """Minimal fake for TmuxRunner's tmux methods used by SessionRegistryService."""

    def __init__(self) -> None:
        self._alive_sessions: set[str] = set()
        self._session_name_prefix = "tgcli_"

    def build_session_name(self, terminal_key: str) -> str:
        return f"tgcli_{terminal_key}"[:64]

    async def session_exists(self, session_name: str) -> bool:
        return session_name in self._alive_sessions

    async def list_managed_sessions(self) -> list[str]:
        return sorted(s for s in self._alive_sessions if s.startswith("tgcli_"))


def _make_registry(tmp_path, *, alive_sessions: set[str] | None = None):
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
    return registry, session_service, cache, tmux


# ── list_active_sessions ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_active_sessions_returns_empty_when_no_tmux(tmp_path) -> None:
    registry, _, _, _ = _make_registry(tmp_path)
    result = await registry.list_active_sessions()
    assert result == []


@pytest.mark.asyncio
async def test_list_active_sessions_returns_tmux_sessions(tmp_path) -> None:
    registry, session_service, cache, tmux = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    # Create a SessionContext with terminal_id
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    ctx = await session_service.get(1)
    # Override terminal_id to match the tmux session
    ctx.terminal_id = "user_1_abc123"
    await session_service._store.save(ctx)
    # Create a SessionState
    cache.get_or_create(
        session_id="s1",
        provider="claude_code",
        workdir="/proj",
        terminal_id="user_1_abc123",
        user_id=1,
    )

    result = await registry.list_active_sessions()
    assert len(result) == 1
    assert result[0].terminal_id == "user_1_abc123"
    assert result[0].workdir == "/proj"
    assert result[0].is_alive is True
    assert result[0].owner_user_id == 1


@pytest.mark.asyncio
async def test_list_active_sessions_includes_attached_users(tmp_path) -> None:
    registry, session_service, _, tmux = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    # Owner
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    owner = await session_service.get(1)
    owner.terminal_id = "user_1_abc123"
    owner.attached_user_ids = [2]
    await session_service._store.save(owner)

    # Attached user
    await session_service.switch(
        user_id=2,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    attached = await session_service.get(2)
    attached.terminal_id = "user_1_abc123"
    attached.is_owner = False
    await session_service._store.save(attached)

    result = await registry.list_active_sessions()
    assert len(result) == 1
    assert 2 in result[0].attached_user_ids


# ── attach_user ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_user_fails_when_session_not_alive(tmp_path) -> None:
    registry, _, _, _ = _make_registry(tmp_path, alive_sessions=set())

    ok, text = await registry.attach_user(user_id=2, terminal_id="nonexistent")
    assert ok is False
    assert "不存在" in text


@pytest.mark.asyncio
async def test_attach_user_succeeds(tmp_path) -> None:
    registry, session_service, _, tmux = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    # Create owner context
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    owner = await session_service.get(1)
    owner.terminal_id = "user_1_abc123"
    await session_service._store.save(owner)

    # Attach user 2
    ok, text = await registry.attach_user(user_id=2, terminal_id="user_1_abc123")
    assert ok is True
    assert "已连接" in text

    # Verify user 2's context
    ctx2 = await session_service.get(2)
    assert ctx2 is not None
    assert ctx2.terminal_id == "user_1_abc123"
    assert ctx2.claude_chat_active is True

    # Verify owner's attached list
    owner = await session_service.get(1)
    assert 2 in owner.attached_user_ids


@pytest.mark.asyncio
async def test_attach_user_noop_if_already_attached(tmp_path) -> None:
    registry, session_service, _, _ = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    owner = await session_service.get(1)
    owner.terminal_id = "user_1_abc123"
    await session_service._store.save(owner)

    # Attach once
    await registry.attach_user(user_id=2, terminal_id="user_1_abc123")
    # Attach again
    ok, text = await registry.attach_user(user_id=2, terminal_id="user_1_abc123")
    assert ok is True
    assert "已连接" in text


# ── detach_user ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detach_user_fails_when_not_attached(tmp_path) -> None:
    registry, _, _, _ = _make_registry(tmp_path)

    ok, text = await registry.detach_user(user_id=1)
    assert ok is False
    assert "未连接" in text


@pytest.mark.asyncio
async def test_detach_user_succeeds(tmp_path) -> None:
    registry, session_service, _, _ = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    # Setup: owner + attached user
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    owner = await session_service.get(1)
    owner.terminal_id = "user_1_abc123"
    await session_service._store.save(owner)

    await registry.attach_user(user_id=2, terminal_id="user_1_abc123")

    # Detach user 2
    ok, text = await registry.detach_user(user_id=2)
    assert ok is True
    assert "已断开" in text

    # Verify user 2's context reset
    ctx2 = await session_service.get(2)
    assert ctx2.claude_chat_active is False

    # Verify owner's attached list updated
    owner = await session_service.get(1)
    assert 2 not in owner.attached_user_ids


# ── validate_or_reattach ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_or_reattach_returns_none_when_no_context(tmp_path) -> None:
    registry, _, _, _ = _make_registry(tmp_path)

    result = await registry.validate_or_reattach(user_id=999)
    assert result is None


@pytest.mark.asyncio
async def test_validate_or_reattach_returns_context_when_alive(tmp_path) -> None:
    registry, session_service, _, _ = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
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

    result = await registry.validate_or_reattach(user_id=1)
    assert result is not None
    assert result.terminal_id == "user_1_abc123"


@pytest.mark.asyncio
async def test_validate_or_reattach_returns_none_when_dead_and_no_recovery(tmp_path) -> None:
    registry, session_service, _, _ = _make_registry(tmp_path, alive_sessions=set())
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

    result = await registry.validate_or_reattach(user_id=1)
    assert result is None


@pytest.mark.asyncio
async def test_validate_or_reattach_binds_live_state_for_same_user_and_workdir(tmp_path) -> None:
    registry, session_service, cache, _ = _make_registry(
        tmp_path,
        alive_sessions={"tgcli_user_1_new456"},
    )
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    ctx = await session_service.get(1)
    ctx.terminal_id = "user_1_old123"
    ctx.claude_session_id = "old-claude-session"
    old_updated_at = utc_now() - timedelta(hours=1)
    ctx.updated_at = old_updated_at
    await session_service._store.save(ctx)

    cache._repository.save(
        SessionState(
            session_id="state-new456",
            user_id=1,
            provider="claude_code",
            workdir="/proj",
            terminal_id="user_1_new456",
            claude_session_id="new-claude-session",
        )
    )

    result = await registry.validate_or_reattach(user_id=1)

    assert result is not None
    assert result.terminal_id == "user_1_new456"
    assert result.claude_session_id == "new-claude-session"
    assert result.updated_at > old_updated_at


@pytest.mark.asyncio
async def test_validate_or_reattach_chooses_most_recent_live_state(tmp_path) -> None:
    registry, session_service, cache, _ = _make_registry(
        tmp_path,
        alive_sessions={"tgcli_user_1_older", "tgcli_user_1_newer"},
    )
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    ctx = await session_service.get(1)
    ctx.terminal_id = "user_1_dead"
    ctx.claude_session_id = "dead-claude-session"
    await session_service._store.save(ctx)

    now = utc_now()
    older = SessionState(
        session_id="aaa-older",
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_id="user_1_older",
        claude_session_id="older-claude-session",
    )
    older.created_at = now - timedelta(minutes=10)
    older.last_activity = now - timedelta(minutes=10)
    older.revision = 1
    newer = SessionState(
        session_id="zzz-newer",
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_id="user_1_newer",
        claude_session_id="newer-claude-session",
    )
    newer.created_at = now - timedelta(minutes=5)
    newer.last_activity = now
    newer.revision = 2
    cache._repository.save(older)
    cache._repository.save(newer)

    result = await registry.validate_or_reattach(user_id=1)

    assert result is not None
    assert result.terminal_id == "user_1_newer"
    assert result.claude_session_id == "newer-claude-session"


# ── get_session_info ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_session_info_returns_none_when_dead(tmp_path) -> None:
    registry, _, _, _ = _make_registry(tmp_path, alive_sessions=set())

    result = await registry.get_session_info("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_get_session_info_returns_info_when_alive(tmp_path) -> None:
    registry, session_service, cache, _ = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    owner = await session_service.get(1)
    owner.terminal_id = "user_1_abc123"
    await session_service._store.save(owner)
    cache.get_or_create(
        session_id="s1",
        provider="claude_code",
        workdir="/proj",
        terminal_id="user_1_abc123",
        user_id=1,
    )

    result = await registry.get_session_info("user_1_abc123")
    assert result is not None
    assert result.terminal_id == "user_1_abc123"
    assert result.workdir == "/proj"
    assert result.owner_user_id == 1
    assert result.is_alive is True
