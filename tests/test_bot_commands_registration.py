"""Tests for bot command registration during startup."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.bootstrap import AppContainer
from app.bot.commands import BOT_COMMANDS
from app.config.settings import Settings


def make_settings(tmp_path) -> Settings:
    return Settings.model_validate(
        {
            "TG_BOT_TOKEN": "123456:TESTTOKEN",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": False,
            "TMUX_DATA_DIR": str(tmp_path),
            "CLAUDE_CLI_BIN": "claude",
            "CLAUDE_INSTALL_HOOKS": False,
            "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
            "CLAUDE_HOOK_SOCKET_PATH": str(tmp_path / "hook.sock"),
            "CLAUDE_JSONL_SYNC_DEBOUNCE_MS": 10,
            "CLAUDE_PERIODIC_RECHECK_MS": 10,
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": str(tmp_path),
        }
    )


@pytest.mark.asyncio
async def test_start_registers_commands(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify set_my_commands is called with BOT_COMMANDS during startup."""
    container = AppContainer(make_settings(tmp_path))

    mock_set_commands = AsyncMock()
    monkeypatch.setattr(container.bot, "set_my_commands", mock_set_commands)
    # Stub out remaining startup steps
    monkeypatch.setattr(container.hook_socket_server, "start", AsyncMock())
    monkeypatch.setattr(container.session_registry, "start_health_check", AsyncMock())
    monkeypatch.setattr(container.upload_cleanup, "start", AsyncMock())
    monkeypatch.setattr(container, "_restore_session_bindings", AsyncMock())
    monkeypatch.setattr(container, "_start_interrupt_watchers", lambda: None)
    monkeypatch.setattr(container, "_start_agent_file_watchers", lambda: None)

    await container.start()

    mock_set_commands.assert_called_once_with(BOT_COMMANDS)
    await container.stop()


@pytest.mark.asyncio
async def test_start_survives_command_registration_failure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify startup completes even when set_my_commands raises."""
    container = AppContainer(make_settings(tmp_path))

    mock_set_commands = AsyncMock(side_effect=RuntimeError("Network error"))
    monkeypatch.setattr(container.bot, "set_my_commands", mock_set_commands)
    # Stub out remaining startup steps
    monkeypatch.setattr(container.hook_socket_server, "start", AsyncMock())
    monkeypatch.setattr(container.session_registry, "start_health_check", AsyncMock())
    monkeypatch.setattr(container.upload_cleanup, "start", AsyncMock())
    monkeypatch.setattr(container, "_restore_session_bindings", AsyncMock())
    monkeypatch.setattr(container, "_start_interrupt_watchers", lambda: None)
    monkeypatch.setattr(container, "_start_agent_file_watchers", lambda: None)

    await container.start()

    # Startup completed despite the exception
    assert container._started is True
    await container.stop()
