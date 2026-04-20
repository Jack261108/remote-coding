import pytest

from app.adapters.cli.factory import CLIAdapterFactory
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.config.settings import Settings


def build_settings() -> Settings:
    return Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": False,
            "CLAUDE_CLI_BIN": "claude",
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": "/tmp",
        }
    )


def test_provider_normalization() -> None:
    factory = CLIAdapterFactory(settings=build_settings(), runner=SubprocessRunner())

    assert factory.normalize_provider("claude") == "claude_code"
    assert factory.normalize_provider("claude-code") == "claude_code"
    assert factory.normalize_provider("codex_cli") == "codex"
    assert factory.normalize_provider("gemini-cli") == "gemini"


def test_provider_invalid() -> None:
    factory = CLIAdapterFactory(settings=build_settings(), runner=SubprocessRunner())

    try:
        factory.normalize_provider("unknown")
    except ValueError as exc:
        assert "不支持 provider" in str(exc)
    else:
        raise AssertionError("expected ValueError")


@pytest.mark.asyncio
async def test_ensure_terminal_when_tmux_disabled() -> None:
    factory = CLIAdapterFactory(settings=build_settings(), runner=SubprocessRunner())
    ok, err = await factory.ensure_terminal(terminal_key="user_1", workdir="/tmp")
    assert ok is False
    assert "CLAUDE_TMUX_MODE" in err


@pytest.mark.asyncio
async def test_ensure_claude_interactive_session_when_tmux_disabled() -> None:
    factory = CLIAdapterFactory(settings=build_settings(), runner=SubprocessRunner())
    ok, err = await factory.ensure_claude_interactive_session(terminal_key="user_1", workdir="/tmp")
    assert ok is False
    assert "CLAUDE_TMUX_MODE" in err


@pytest.mark.asyncio
async def test_ensure_terminal_when_tmux_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings().model_copy(update={"claude_tmux_mode": True})
    tmux = TmuxRunner()

    async def fake_ensure_terminal(*, terminal_key: str, workdir: str):
        return terminal_key == "user_1" and workdir == "/tmp", ""

    monkeypatch.setattr(tmux, "ensure_terminal", fake_ensure_terminal)
    factory = CLIAdapterFactory(settings=settings, runner=SubprocessRunner(), tmux_runner=tmux)

    ok, err = await factory.ensure_terminal(terminal_key="user_1", workdir="/tmp")
    assert ok is True
    assert err == ""


@pytest.mark.asyncio
async def test_ensure_claude_interactive_session_when_tmux_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings().model_copy(update={"claude_tmux_mode": True})
    tmux = TmuxRunner()

    async def fake_ensure_interactive(*, terminal_key: str, workdir: str):
        return terminal_key == "user_1" and workdir == "/tmp", ""

    monkeypatch.setattr(tmux, "ensure_claude_interactive_session", fake_ensure_interactive)
    factory = CLIAdapterFactory(settings=settings, runner=SubprocessRunner(), tmux_runner=tmux)

    ok, err = await factory.ensure_claude_interactive_session(terminal_key="user_1", workdir="/tmp")
    assert ok is True
    assert err == ""


@pytest.mark.asyncio
async def test_user_question_tmux_actions_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings().model_copy(update={"claude_tmux_mode": True})
    tmux = TmuxRunner()

    async def fake_select(*, terminal_key: str, workdir: str, option_index: int, submit_after: bool = False):
        return terminal_key == "user_1" and workdir == "/tmp" and option_index == 2 and submit_after, ""

    async def fake_text(*, terminal_key: str, workdir: str, option_count: int, text: str, submit_after: bool = False):
        return terminal_key == "user_1" and workdir == "/tmp" and option_count == 3 and text == "自定义日期" and not submit_after, ""

    async def fake_advance(*, terminal_key: str, workdir: str, final_question: bool):
        return terminal_key == "user_1" and workdir == "/tmp" and final_question, ""

    monkeypatch.setattr(tmux, "select_user_question_option", fake_select)
    monkeypatch.setattr(tmux, "answer_user_question_with_text", fake_text)
    monkeypatch.setattr(tmux, "advance_user_question_after_multi_select", fake_advance)

    factory = CLIAdapterFactory(settings=settings, runner=SubprocessRunner(), tmux_runner=tmux)

    ok, err = await factory.select_claude_user_question_option(
        terminal_key="user_1",
        workdir="/tmp",
        option_index=2,
        submit_after=True,
    )
    assert ok is True
    assert err == ""

    ok, err = await factory.answer_claude_user_question_with_text(
        terminal_key="user_1",
        workdir="/tmp",
        option_count=3,
        text="自定义日期",
        submit_after=False,
    )
    assert ok is True
    assert err == ""

    ok, err = await factory.advance_claude_user_question_after_multi_select(
        terminal_key="user_1",
        workdir="/tmp",
        final_question=True,
    )
    assert ok is True
    assert err == ""
