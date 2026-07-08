import pytest

from app.adapters.cli.registry import CLIAdapterRegistry
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.config.settings import Settings


def build_settings(*, claude_tmux_mode: bool = False) -> Settings:
    return Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": claude_tmux_mode,
            "CLAUDE_CLI_BIN": "claude",
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": "/tmp",
        }
    )


def test_provider_aliases_and_available_providers() -> None:
    registry = CLIAdapterRegistry(settings=build_settings(), runner=SubprocessRunner())

    assert registry.normalize_provider("claude") == "claude_code"
    assert registry.normalize_provider("claude-code") == "claude_code"
    assert registry.normalize_provider("claude_code") == "claude_code"
    assert registry.normalize_provider("codex_cli") == "codex"
    assert registry.normalize_provider("codex-cli") == "codex"
    assert registry.normalize_provider("gemini_cli") == "gemini"
    assert registry.normalize_provider("gemini-cli") == "gemini"
    assert registry.available_providers() == ["claude_code", "codex", "gemini"]


def test_unknown_provider_keeps_legacy_error_text() -> None:
    registry = CLIAdapterRegistry(settings=build_settings(), runner=SubprocessRunner())

    with pytest.raises(ValueError, match="不支持 provider: unknown"):
        registry.normalize_provider("unknown")


def test_capabilities_when_claude_terminal_disabled() -> None:
    registry = CLIAdapterRegistry(settings=build_settings(), runner=SubprocessRunner())

    claude_capabilities = registry.capabilities("claude")
    assert claude_capabilities.run_task is True
    assert claude_capabilities.cancel_task is True
    assert claude_capabilities.persistent_terminal is False
    assert claude_capabilities.interactive_input is False
    assert claude_capabilities.claude_resume is False
    assert claude_capabilities.user_question_tui is False
    assert claude_capabilities.session_state is False

    assert registry.capabilities("codex").persistent_terminal is False
    assert registry.capabilities("gemini").persistent_terminal is False


def test_capabilities_when_claude_terminal_enabled() -> None:
    registry = CLIAdapterRegistry(settings=build_settings(claude_tmux_mode=True), runner=SubprocessRunner(), tmux_runner=TmuxRunner())

    claude_capabilities = registry.capabilities("claude")
    assert claude_capabilities.persistent_terminal is True
    assert claude_capabilities.interactive_input is True
    assert claude_capabilities.claude_resume is True
    assert claude_capabilities.user_question_tui is True
    assert claude_capabilities.session_state is True
    assert registry.claude_terminal_enabled is True
