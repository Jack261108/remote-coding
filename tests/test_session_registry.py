from __future__ import annotations

import asyncio
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
        self.closed_terminal_keys: list[str] = []

    def build_session_name(self, terminal_key: str) -> str:
        return f"tgcli_{terminal_key}"[:64]

    async def session_exists(self, session_name: str) -> bool:
        return session_name in self._alive_sessions

    async def list_managed_sessions(self) -> list[str]:
        return sorted(s for s in self._alive_sessions if s.startswith("tgcli_"))

    async def close_terminal(self, terminal_key: str) -> bool:
        self.closed_terminal_keys.append(terminal_key)
        session_name = self.build_session_name(terminal_key)
        if session_name not in self._alive_sessions:
            return False
        self._alive_sessions.remove(session_name)
        return True


class RecordingAutoApproveService:
    def __init__(self) -> None:
        self.cleared_session_ids: list[str] = []

    async def clear_session(self, session_id: str) -> None:
        self.cleared_session_ids.append(session_id)


def _make_registry(tmp_path, *, alive_sessions: set[str] | None = None, auto_approve_service=None):
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
        auto_approve_service=auto_approve_service,
    )
    return registry, session_service, cache, tmux


async def _seed_terminal_group(session_service: SessionService, terminal_id: str = "user_1_abc123") -> None:
    owner, _ = await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    owner.terminal_id = terminal_id
    owner.claude_session_id = "claude-owner"
    owner.attached_user_ids = [2]
    await session_service.save_session_context(owner)

    attached, _ = await session_service.switch(
        user_id=2,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    attached.terminal_id = terminal_id
    attached.claude_session_id = "claude-attached"
    attached.is_owner = False
    await session_service.save_session_context(attached)


async def _assert_terminal_group_cleared(session_service: SessionService) -> None:
    owner = await session_service.get(1)
    attached = await session_service.get(2)
    assert owner is not None
    assert owner.terminal_mode is False
    assert owner.terminal_id is None
    assert owner.claude_chat_active is False
    assert owner.claude_session_id is None
    assert owner.attached_user_ids == []
    assert owner.is_owner is True
    assert attached is not None
    assert attached.terminal_mode is False
    assert attached.terminal_id is None
    assert attached.claude_chat_active is False
    assert attached.claude_session_id is None
    assert attached.attached_user_ids == []
    assert attached.is_owner is True


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


# ── close_session / health cleanup ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_session_cleans_owner_and_attached_contexts_after_success(tmp_path) -> None:
    registry, session_service, _, tmux = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    await _seed_terminal_group(session_service)

    ok = await registry.close_session("user_1_abc123")

    assert ok is True
    assert tmux.closed_terminal_keys == ["user_1_abc123"]
    assert "tgcli_user_1_abc123" not in tmux._alive_sessions
    await _assert_terminal_group_cleared(session_service)


@pytest.mark.asyncio
async def test_close_session_keeps_contexts_when_tmux_close_fails(tmp_path) -> None:
    registry, session_service, _, tmux = _make_registry(tmp_path, alive_sessions=set())
    await _seed_terminal_group(session_service)

    ok = await registry.close_session("user_1_abc123")

    assert ok is False
    assert tmux.closed_terminal_keys == ["user_1_abc123"]
    owner = await session_service.get(1)
    attached = await session_service.get(2)
    assert owner is not None
    assert owner.terminal_id == "user_1_abc123"
    assert owner.terminal_mode is True
    assert owner.claude_chat_active is True
    assert owner.claude_session_id == "claude-owner"
    assert owner.attached_user_ids == [2]
    assert attached is not None
    assert attached.terminal_id == "user_1_abc123"
    assert attached.terminal_mode is True
    assert attached.claude_chat_active is True
    assert attached.claude_session_id == "claude-attached"
    assert attached.is_owner is False


@pytest.mark.asyncio
async def test_close_session_serializes_against_concurrent_attach(tmp_path) -> None:
    registry, session_service, _, _ = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_abc123"})
    await _seed_terminal_group(session_service)
    attach_ready = asyncio.Event()
    allow_attach = asyncio.Event()
    original_switch = session_service.switch

    async def delayed_switch(*args, **kwargs):
        if kwargs.get("user_id") == 3:
            attach_ready.set()
            await allow_attach.wait()
        return await original_switch(*args, **kwargs)

    session_service.switch = delayed_switch  # type: ignore[method-assign]

    attach_task = asyncio.create_task(registry.attach_user(user_id=3, terminal_id="user_1_abc123"))
    await asyncio.wait_for(attach_ready.wait(), timeout=1)
    close_task = asyncio.create_task(registry.close_session("user_1_abc123"))
    await asyncio.sleep(0)
    allow_attach.set()

    attach_ok, _ = await attach_task
    close_ok = await close_task

    assert attach_ok is True
    assert close_ok is True
    await _assert_terminal_group_cleared(session_service)
    attached_late = await session_service.get(3)
    assert attached_late is not None
    assert attached_late.terminal_mode is False
    assert attached_late.terminal_id is None
    assert attached_late.claude_chat_active is False
    assert attached_late.claude_session_id is None
    assert attached_late.is_owner is True


@pytest.mark.asyncio
async def test_detach_user_does_not_restore_closed_owner(tmp_path) -> None:
    registry, session_service, _, _ = _make_registry(tmp_path, alive_sessions={"tgcli_user_1_old"})
    await _seed_terminal_group(session_service, terminal_id="user_1_old")
    close_ready = asyncio.Event()
    allow_old_owner_save = asyncio.Event()
    original_save = session_service.save_session_context

    async def delayed_save(session):
        if session.user_id == 1 and session.terminal_id == "user_1_old" and session.attached_user_ids == []:
            close_ready.set()
            await allow_old_owner_save.wait()
        await original_save(session)

    session_service.save_session_context = delayed_save  # type: ignore[method-assign]

    detach_task = asyncio.create_task(registry.detach_user(user_id=2))
    await asyncio.wait_for(close_ready.wait(), timeout=1)
    close_task = asyncio.create_task(registry.close_session("user_1_old"))
    await asyncio.sleep(0)
    assert close_task.done() is False
    allow_old_owner_save.set()

    detach_ok, _ = await detach_task
    close_ok = await close_task

    assert detach_ok is True
    assert close_ok is True
    old_owner = await session_service.get(1)
    detached_user = await session_service.get(2)
    assert old_owner is not None
    assert old_owner.terminal_mode is False
    assert old_owner.terminal_id is None
    assert old_owner.claude_chat_active is False
    assert old_owner.claude_session_id is None
    assert old_owner.attached_user_ids == []
    assert detached_user is not None
    assert detached_user.terminal_mode is False
    assert detached_user.terminal_id is None
    assert detached_user.claude_chat_active is False


@pytest.mark.asyncio
async def test_attach_to_new_terminal_does_not_restore_closed_previous_owner(tmp_path) -> None:
    registry, session_service, _, _ = _make_registry(
        tmp_path,
        alive_sessions={"tgcli_user_1_old", "tgcli_user_3_new"},
    )
    await _seed_terminal_group(session_service, terminal_id="user_1_old")
    close_ready = asyncio.Event()
    allow_old_owner_save = asyncio.Event()
    original_save = session_service.save_session_context

    async def delayed_save(session):
        if session.user_id == 1 and session.terminal_id == "user_1_old" and session.attached_user_ids == []:
            close_ready.set()
            await allow_old_owner_save.wait()
        await original_save(session)

    session_service.save_session_context = delayed_save  # type: ignore[method-assign]

    attach_task = asyncio.create_task(registry.attach_user(user_id=2, terminal_id="user_3_new"))
    await asyncio.wait_for(close_ready.wait(), timeout=1)
    close_task = asyncio.create_task(registry.close_session("user_1_old"))
    await asyncio.sleep(0)
    assert close_task.done() is False
    allow_old_owner_save.set()

    attach_ok, _ = await attach_task
    close_ok = await close_task

    assert attach_ok is True
    assert close_ok is True
    old_owner = await session_service.get(1)
    moved_user = await session_service.get(2)
    assert old_owner is not None
    assert old_owner.terminal_mode is False
    assert old_owner.terminal_id is None
    assert old_owner.claude_chat_active is False
    assert old_owner.claude_session_id is None
    assert old_owner.attached_user_ids == []
    assert moved_user is not None
    assert moved_user.terminal_id == "user_3_new"
    assert moved_user.claude_chat_active is True


@pytest.mark.asyncio
async def test_close_session_clears_claude_session_bound_during_tmux_close(tmp_path) -> None:
    auto_approve = RecordingAutoApproveService()
    registry, session_service, _, tmux = _make_registry(
        tmp_path,
        alive_sessions={"tgcli_user_1_abc123"},
        auto_approve_service=auto_approve,
    )
    await _seed_terminal_group(session_service)

    async def close_and_bind(terminal_key: str) -> bool:
        owner = await session_service.get(1)
        assert owner is not None
        owner.claude_session_id = "claude-bound-during-close"
        await session_service.save_session_context(owner)
        tmux._alive_sessions.discard(tmux.build_session_name(terminal_key))
        return True

    tmux.close_terminal = close_and_bind

    ok = await registry.close_session("user_1_abc123")

    assert ok is True
    assert "claude-bound-during-close" in auto_approve.cleared_session_ids
    await _assert_terminal_group_cleared(session_service)


@pytest.mark.asyncio
async def test_health_check_skips_cleanup_when_terminal_revives_before_clear(tmp_path) -> None:
    registry, session_service, _, tmux = _make_registry(tmp_path, alive_sessions=set())
    await _seed_terminal_group(session_service)
    calls = 0

    async def flapping_session_exists(session_name: str) -> bool:
        nonlocal calls
        if session_name == "tgcli_user_1_abc123":
            calls += 1
            return calls > 1
        return await FakeTmuxRunner.session_exists(tmux, session_name)

    tmux.session_exists = flapping_session_exists

    await registry._run_health_check()

    owner = await session_service.get(1)
    attached = await session_service.get(2)
    assert owner is not None
    assert owner.terminal_mode is True
    assert owner.terminal_id == "user_1_abc123"
    assert owner.claude_chat_active is True
    assert owner.claude_session_id == "claude-owner"
    assert owner.attached_user_ids == [2]
    assert attached is not None
    assert attached.terminal_mode is True
    assert attached.terminal_id == "user_1_abc123"
    assert attached.claude_chat_active is True
    assert attached.claude_session_id == "claude-attached"
    assert attached.is_owner is False


@pytest.mark.asyncio
async def test_health_check_cleans_dead_terminal_group_owner_and_attached_contexts(tmp_path) -> None:
    auto_approve = RecordingAutoApproveService()
    registry, session_service, _, _ = _make_registry(
        tmp_path,
        alive_sessions={"tgcli_user_3_other"},
        auto_approve_service=auto_approve,
    )
    await _seed_terminal_group(session_service)
    other, _ = await session_service.switch(
        user_id=3,
        provider="claude_code",
        workdir="/other",
        terminal_mode=True,
        claude_chat_active=True,
    )
    other.terminal_id = "user_3_other"
    other.claude_session_id = "claude-other"
    await session_service.save_session_context(other)

    await registry._run_health_check()

    await _assert_terminal_group_cleared(session_service)
    assert set(auto_approve.cleared_session_ids) == {"claude-owner", "claude-attached"}
    other = await session_service.get(3)
    assert other is not None
    assert other.terminal_mode is True
    assert other.terminal_id == "user_3_other"
    assert other.claude_chat_active is True
    assert other.claude_session_id == "claude-other"
    assert other.is_owner is True


@pytest.mark.asyncio
async def test_reconcile_closes_ownerless_managed_tmux_session(tmp_path) -> None:
    registry, session_service, _, tmux = _make_registry(
        tmp_path,
        alive_sessions={"tgcli_user_1_kept", "tgcli_user_orphan"},
    )
    kept, _ = await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    kept.terminal_id = "user_1_kept"
    await session_service.save_session_context(kept)

    await registry.reconcile_terminal_lifecycle()

    assert tmux.closed_terminal_keys == ["user_orphan"]
    assert "tgcli_user_orphan" not in tmux._alive_sessions
    assert "tgcli_user_1_kept" in tmux._alive_sessions


@pytest.mark.asyncio
async def test_reconcile_does_not_close_orphan_when_bound_during_recheck(tmp_path) -> None:
    registry, session_service, _, tmux = _make_registry(tmp_path, alive_sessions={"tgcli_user_late"})
    original_list_all = session_service.list_all
    calls = 0

    async def binding_on_second_list():
        nonlocal calls
        calls += 1
        if calls == 2:
            late, _ = await session_service.switch(
                user_id=2,
                provider="claude_code",
                workdir="/late",
                terminal_mode=True,
                claude_chat_active=True,
            )
            late.terminal_id = "user_late"
            await session_service.save_session_context(late)
        return await original_list_all()

    session_service.list_all = binding_on_second_list  # type: ignore[method-assign]

    await registry.reconcile_terminal_lifecycle()

    assert tmux.closed_terminal_keys == []
    assert "tgcli_user_late" in tmux._alive_sessions


@pytest.mark.asyncio
async def test_reconcile_logs_warning_when_orphan_close_fails(tmp_path, caplog) -> None:
    import logging

    registry, session_service, _, tmux = _make_registry(
        tmp_path,
        alive_sessions={"tgcli_user_1_kept", "tgcli_user_orphan"},
    )
    kept, _ = await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    kept.terminal_id = "user_1_kept"
    await session_service.save_session_context(kept)

    async def failing_close_terminal(terminal_key: str):
        # Record the call but never remove the session from _alive_sessions,
        # and report a structured failure (tuple) as the real adapter can.
        tmux.closed_terminal_keys.append(terminal_key)
        return (False, "busy")

    tmux.close_terminal = failing_close_terminal  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="app.services.session_registry"):
        await registry.reconcile_terminal_lifecycle()

    # Orphan must remain alive because close failed.
    assert "tgcli_user_orphan" in tmux._alive_sessions
    assert tmux.closed_terminal_keys == ["user_orphan"]

    warning_records = [
        r for r in caplog.records if r.levelno == logging.WARNING and "failed to close orphaned tmux session" in r.getMessage()
    ]
    assert len(warning_records) == 1
    assert warning_records[0].reason == "busy"
    assert warning_records[0].terminal_id == "user_orphan"
    assert warning_records[0].tmux_session == "tgcli_user_orphan"


@pytest.mark.asyncio
async def test_reconcile_handles_non_tuple_close_result(tmp_path) -> None:
    registry, session_service, _, tmux = _make_registry(
        tmp_path,
        alive_sessions={"tgcli_user_1_kept", "tgcli_user_orphan"},
    )
    kept, _ = await session_service.switch(
        user_id=1,
        provider="claude_code",
        workdir="/proj",
        terminal_mode=True,
        claude_chat_active=True,
    )
    kept.terminal_id = "user_1_kept"
    await session_service.save_session_context(kept)

    async def bool_close_terminal(terminal_key: str):
        # Return a bare bool (non-tuple) to exercise _close_terminal_result's
        # isinstance=False branch; never remove the session.
        tmux.closed_terminal_keys.append(terminal_key)
        return False

    tmux.close_terminal = bool_close_terminal  # type: ignore[method-assign]

    # Must not raise.
    await registry.reconcile_terminal_lifecycle()

    # Orphan remains because close reported failure.
    assert "tgcli_user_orphan" in tmux._alive_sessions
    assert tmux.closed_terminal_keys == ["user_orphan"]
