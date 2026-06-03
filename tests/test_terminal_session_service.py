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


@pytest.mark.asyncio
async def test_resolve_for_task_enables_interactive_only_for_active_claude_chat(tmp_path: Path) -> None:
    service, session_service, _, _ = make_terminal_service(tmp_path, claude_tmux_mode=True)
    session = await session_service.get_or_create(
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
async def test_open_claude_chat_session_rejects_explicit_workdir_outside_allowlist(tmp_path: Path) -> None:
    service, _, factory, cleared_users = make_terminal_service(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="workdir 不在 ALLOWED_WORKDIRS 白名单内"):
        await service.open_claude_chat_session(1, workdir=str(outside))

    assert factory._ensured_interactive_terminal_key is None
    assert cleared_users == [1]
