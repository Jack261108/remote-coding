import asyncio
from pathlib import Path

import pytest

from app.services.terminal_session_service import TerminalSessionService
from tests.fakes.cli import StubAdapter, StubFactory, expected_terminal_id, make_file_backed_session_service, make_settings


def make_terminal_service(tmp_path: Path, *, claude_tmux_mode: bool = True):
    factory = StubFactory(StubAdapter(events=[]))
    session_service = make_file_backed_session_service(tmp_path)
    cleared_users: list[int] = []
    service = TerminalSessionService(
        settings=make_settings(tmp_path, claude_tmux_mode=claude_tmux_mode),
        session_service=session_service,
        cli_factory=factory,
        clear_user_questions=cleared_users.append,
    )
    return service, session_service, factory, cleared_users


class RecordingAutoApproveService:
    def __init__(self) -> None:
        self.cleared_session_ids: list[str] = []

    async def clear_session(self, session_id: str) -> None:
        self.cleared_session_ids.append(session_id)


@pytest.mark.asyncio
async def test_resolve_for_task_enables_interactive_only_for_active_claude_chat(tmp_path: Path) -> None:
    service, session_service, _, _ = make_terminal_service(tmp_path, claude_tmux_mode=True)
    session, _ = await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=True,
        claude_chat_active=True,
    )

    context = await service.resolve_for_task(user_id=1, provider="claude_code", workdir=str(tmp_path))

    assert context.session.session_id == session.session_id
    assert context.terminal_key == expected_terminal_id(user_id=1, workdir=str(tmp_path))
    assert context.interactive is True


@pytest.mark.asyncio
async def test_resolve_for_task_does_not_use_terminal_for_non_claude_provider(tmp_path: Path) -> None:
    service, _, _, _ = make_terminal_service(tmp_path, claude_tmux_mode=True)

    context = await service.resolve_for_task(user_id=1, provider="codex", workdir=str(tmp_path))

    assert context.session.provider == "codex"
    assert context.session.terminal_mode is False
    assert context.terminal_key is None
    assert context.interactive is False


@pytest.mark.asyncio
async def test_ensure_and_reveal_terminal_reports_reveal_failure_as_soft_success(tmp_path: Path) -> None:
    service, _, factory, _ = make_terminal_service(tmp_path)

    async def failed_reveal_terminal(terminal_key: str) -> tuple[bool, str]:
        factory._revealed_terminal_key = terminal_key
        return False, "Terminal 未打开"

    factory.reveal_terminal = failed_reveal_terminal

    ensured, text = await service.ensure_and_reveal_terminal(
        terminal_id="terminal-1",
        workdir=str(tmp_path),
        reveal=True,
        interactive=False,
    )

    assert ensured is True
    assert text == "未能自动打开桌面终端: Terminal 未打开"
    assert factory._ensured_terminal_key == "terminal-1"
    assert factory._ensured_workdir == str(tmp_path)
    assert factory._revealed_terminal_key == "terminal-1"


@pytest.mark.asyncio
async def test_ensure_and_reveal_terminal_uses_interactive_session_when_requested(tmp_path: Path) -> None:
    service, _, factory, _ = make_terminal_service(tmp_path)

    ensured, text = await service.ensure_and_reveal_terminal(
        terminal_id="terminal-1",
        workdir=str(tmp_path),
        reveal=False,
        interactive=True,
    )

    assert ensured is True
    assert text == ""
    assert factory._ensured_interactive_terminal_key == "terminal-1"
    assert factory._ensured_interactive_workdir == str(tmp_path)
    assert factory._revealed_terminal_key is None


@pytest.mark.asyncio
async def test_close_terminal_without_terminal_clears_auto_approve(tmp_path: Path) -> None:
    auto_approve = RecordingAutoApproveService()
    service, session_service, factory, cleared_users = make_terminal_service(tmp_path)
    service._auto_approve_service = auto_approve
    await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=False,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session")

    closed, text = await service.close_terminal(1)

    assert closed is True
    assert text == "Claude 会话已退出"
    assert factory._closed_terminal_key is None
    assert cleared_users == [1]
    assert auto_approve.cleared_session_ids == ["claude-session"]


@pytest.mark.asyncio
async def test_close_terminal_rereads_context_after_waiting_for_terminal_lock(tmp_path: Path) -> None:
    service, session_service, factory, cleared_users = make_terminal_service(tmp_path)
    await _seed_terminal_group(session_service, terminal_id="user_1_old")
    await session_service.switch(user_id=3, provider="claude_code", workdir="/new", terminal_mode=True, claude_chat_active=True)
    new_owner = await session_service.get(3)
    assert new_owner is not None
    new_owner.terminal_id = "user_3_new"
    await session_service.save_session_context(new_owner)
    lock = session_service.terminal_group_lock("user_1_old")
    release_lock = asyncio.Event()

    async def hold_lock() -> None:
        async with lock:
            await release_lock.wait()

    holder = asyncio.create_task(hold_lock())
    await asyncio.sleep(0)
    close_task = asyncio.create_task(service.close_terminal(1))
    await asyncio.sleep(0)
    await session_service.switch(user_id=1, provider="claude_code", workdir="/new", terminal_mode=True, claude_chat_active=True)
    moved = await session_service.get(1)
    assert moved is not None
    moved.terminal_id = "user_3_new"
    moved.is_owner = False
    await session_service.save_session_context(moved)
    release_lock.set()

    closed, text = await close_task
    await holder

    assert closed is True
    assert text == "终端已关闭"
    assert factory._closed_terminal_key == "user_3_new"
    assert set(cleared_users) == {1, 3}
    old_owner = await session_service.get(2)
    assert old_owner is not None
    assert old_owner.terminal_id == "user_1_old"
    assert old_owner.claude_chat_active is True


@pytest.mark.asyncio
async def test_close_terminal_without_terminal_exits_claude_chat(tmp_path: Path) -> None:
    service, session_service, factory, cleared_users = make_terminal_service(tmp_path)
    await session_service.get_or_create(
        user_id=1,
        provider="claude_code",
        workdir=str(tmp_path),
        terminal_mode=False,
        claude_chat_active=True,
    )
    await session_service.bind_claude_session(user_id=1, claude_session_id="claude-session")

    closed, text = await service.close_terminal(1)
    session = await session_service.get(1)

    assert closed is True
    assert text == "Claude 会话已退出"
    assert factory._closed_terminal_key is None
    assert cleared_users == [1]
    assert session is not None
    assert session.claude_chat_active is False
    assert session.claude_session_id is None


@pytest.mark.asyncio
async def test_open_claude_chat_session_creates_terminal_without_previous_session(tmp_path: Path) -> None:
    service, session_service, factory, cleared_users = make_terminal_service(tmp_path)

    opened, text = await service.open_claude_chat_session(1)
    session = await session_service.get(1)

    expected = expected_terminal_id(user_id=1, workdir=str(tmp_path.resolve()))
    assert opened is True
    assert text.startswith("Claude 会话已开启")
    assert factory._closed_terminal_key is None
    assert factory._ensured_interactive_terminal_key == expected
    assert factory._ensured_interactive_workdir == str(tmp_path.resolve())
    assert factory._revealed_terminal_key == expected
    assert cleared_users == [1]
    assert session is not None
    assert session.provider == "claude_code"
    assert session.workdir == str(tmp_path.resolve())
    assert session.terminal_mode is True
    assert session.terminal_id == expected
    assert session.claude_chat_active is True


@pytest.mark.asyncio
async def test_open_claude_chat_session_rebuild_clears_auto_approve_when_old_terminal_missing(tmp_path: Path) -> None:
    auto_approve = RecordingAutoApproveService()
    service, session_service, factory, _ = make_terminal_service(tmp_path)
    service._auto_approve_service = auto_approve
    await _seed_terminal_group(session_service)

    async def missing_terminal(terminal_key: str) -> tuple[bool, str]:
        factory._closed_terminal_key = terminal_key
        return False, "终端不存在"

    factory.close_terminal = missing_terminal

    opened, text = await service.open_claude_chat_session(1)

    assert opened is True
    assert text.startswith("Claude 会话已重建")
    assert factory._closed_terminal_key == "user_1_abc123"
    assert set(auto_approve.cleared_session_ids) == {"claude-owner", "claude-attached"}
    attached = await session_service.get(2)
    assert attached is not None
    assert attached.terminal_mode is False
    assert attached.terminal_id is None
    assert attached.claude_chat_active is False
    assert attached.claude_session_id is None
    assert attached.attached_user_ids == []
    assert attached.is_owner is True


@pytest.mark.asyncio
async def test_open_claude_chat_session_rebuild_keeps_group_when_old_terminal_close_fails(tmp_path: Path) -> None:
    auto_approve = RecordingAutoApproveService()
    service, session_service, factory, cleared_users = make_terminal_service(tmp_path)
    service._auto_approve_service = auto_approve
    await _seed_terminal_group(session_service)

    async def failed_close_terminal(terminal_key: str) -> tuple[bool, str]:
        factory._closed_terminal_key = terminal_key
        return False, "关闭失败"

    factory.close_terminal = failed_close_terminal

    opened, text = await service.open_claude_chat_session(1)

    assert opened is False
    assert text == "旧终端关闭失败: 关闭失败"
    assert factory._closed_terminal_key == "user_1_abc123"
    assert auto_approve.cleared_session_ids == []
    assert cleared_users == [1]
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
async def test_open_claude_chat_session_rebuild_clears_old_terminal_auto_approve(tmp_path: Path) -> None:
    auto_approve = RecordingAutoApproveService()
    service, session_service, factory, cleared_users = make_terminal_service(tmp_path)
    service._auto_approve_service = auto_approve
    await _seed_terminal_group(session_service)

    opened, text = await service.open_claude_chat_session(1)

    assert opened is True
    assert text.startswith("Claude 会话已重建")
    assert factory._closed_terminal_key == "user_1_abc123"
    assert set(auto_approve.cleared_session_ids) == {"claude-owner", "claude-attached"}
    assert 1 in cleared_users


@pytest.mark.asyncio
async def test_open_claude_chat_session_rejects_explicit_workdir_outside_allowlist(tmp_path: Path) -> None:
    service, _, factory, cleared_users = make_terminal_service(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="workdir 不在 ALLOWED_WORKDIRS 白名单内"):
        await service.open_claude_chat_session(1, workdir=str(outside))

    assert factory._ensured_interactive_terminal_key is None
    assert cleared_users == [1]


async def _seed_terminal_group(session_service, terminal_id: str = "user_1_abc123") -> None:
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


async def _assert_terminal_group_cleared(session_service) -> None:
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


@pytest.mark.asyncio
async def test_cleanup_orphaned_terminal_clears_group_auto_approve_and_questions(tmp_path: Path) -> None:
    auto_approve = RecordingAutoApproveService()
    service, session_service, _, cleared_users = make_terminal_service(tmp_path)
    service._auto_approve_service = auto_approve
    await _seed_terminal_group(session_service)

    await service.cleanup_orphaned_terminal(
        "user_1_abc123",
        claude_session_id="claude-owner",
        user_id=1,
    )

    await _assert_terminal_group_cleared(session_service)
    assert set(auto_approve.cleared_session_ids) == {"claude-owner", "claude-attached"}
    assert set(cleared_users) == {1, 2}


@pytest.mark.asyncio
async def test_close_terminal_cleans_attached_terminal_group(tmp_path: Path) -> None:
    service, session_service, factory, cleared_users = make_terminal_service(tmp_path)
    await _seed_terminal_group(session_service)

    closed, text = await service.close_terminal(1)

    assert closed is True
    assert text == "终端已关闭"
    assert factory._closed_terminal_key == "user_1_abc123"
    await _assert_terminal_group_cleared(session_service)
    assert set(cleared_users) == {1, 2}


@pytest.mark.asyncio
async def test_close_terminal_clears_auto_approve_for_terminal_group(tmp_path: Path) -> None:
    auto_approve = RecordingAutoApproveService()
    service, session_service, factory, _ = make_terminal_service(tmp_path)
    service._auto_approve_service = auto_approve
    await _seed_terminal_group(session_service)

    closed, text = await service.close_terminal(1)

    assert closed is True
    assert text == "终端已关闭"
    assert factory._closed_terminal_key == "user_1_abc123"
    assert set(auto_approve.cleared_session_ids) == {"claude-owner", "claude-attached"}


@pytest.mark.asyncio
async def test_close_terminal_keeps_attached_group_when_tmux_close_fails(tmp_path: Path) -> None:
    service, session_service, factory, cleared_users = make_terminal_service(tmp_path)
    await _seed_terminal_group(session_service)

    async def failed_close_terminal(terminal_key: str) -> tuple[bool, str]:
        factory._closed_terminal_key = terminal_key
        return False, "终端关闭失败"

    factory.close_terminal = failed_close_terminal

    closed, text = await service.close_terminal(1)

    assert closed is False
    assert text == "终端关闭失败"
    assert factory._closed_terminal_key == "user_1_abc123"
    assert cleared_users == []
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
