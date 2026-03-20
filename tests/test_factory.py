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
