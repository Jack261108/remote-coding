from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.bot.middleware.auth import AuthMiddleware
from app.bot.middleware.rate_limit import RateLimitMiddleware
from app.config.settings import Settings


class DummyCallbackQuery:
    def __init__(self, user_id: int | None = 1) -> None:
        self.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
        self.answers: list[str] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append(text)


async def _passing_handler(event, data):
    data["called"] = True
    return "ok"


def test_settings_allow_all_users_star() -> None:
    settings = Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "*",
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

    assert settings.allow_all_users is True
    assert settings.allowed_user_id_set == set()


def test_auth_middleware_allow_all_flag() -> None:
    middleware = AuthMiddleware(set(), allow_all_users=True)
    assert middleware is not None


def test_settings_parse_claude_hook_fields() -> None:
    settings = Settings.model_validate(
        {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_USER_IDS": "1",
            "DEFAULT_PROVIDER": "claude_code",
            "DEFAULT_TIMEOUT_SEC": 10,
            "MAX_CONCURRENT_TASKS": 1,
            "CLAUDE_TMUX_MODE": False,
            "CLAUDE_CLI_BIN": "claude",
            "CLAUDE_CONFIG_DIR": " ~/.config/claude ",
            "CLAUDE_HOOK_SOCKET_PATH": "/tmp/remote-coding.sock",
            "CLAUDE_INSTALL_HOOKS": "true",
            "CLAUDE_HOOK_MAX_MESSAGE_BYTES": 2048,
            "CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC": 30,
            "CLAUDE_HOOK_MAX_PENDING_PERMISSIONS": 4,
            "CLAUDE_JSONL_SYNC_DEBOUNCE_MS": 250,
            "CLAUDE_PERIODIC_RECHECK_MS": 750,
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": "/tmp",
        }
    )

    assert settings.claude_config_dir == "~/.config/claude"
    assert settings.claude_hook_socket_path == "/tmp/remote-coding.sock"
    assert settings.claude_install_hooks is True
    assert settings.claude_hook_max_message_bytes == 2048
    assert settings.claude_hook_pending_permission_ttl_sec == 30
    assert settings.claude_hook_max_pending_permissions == 4
    assert settings.claude_jsonl_sync_debounce_ms == 250
    assert settings.claude_periodic_recheck_ms == 750


def test_settings_rejects_non_positive_claude_hook_limits() -> None:
    base_payload = {
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

    for field in (
        "CLAUDE_HOOK_MAX_MESSAGE_BYTES",
        "CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC",
        "CLAUDE_HOOK_MAX_PENDING_PERMISSIONS",
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**base_payload, field: 0})


def test_env_example_matches_supported_claude_settings() -> None:
    content = (Path(__file__).resolve().parents[1] / "deploy" / "env" / ".env.example").read_text(encoding="utf-8")

    assert "BRIDGE_WS_" not in content
    assert "CLAUDE_CONFIG_DIR=" in content
    assert "CLAUDE_HOOK_SOCKET_PATH=/tmp/remote-coding-claude.sock" in content
    assert "CLAUDE_INSTALL_HOOKS=true" in content
    assert "CLAUDE_HOOK_MAX_MESSAGE_BYTES=1048576" in content
    assert "CLAUDE_HOOK_PENDING_PERMISSION_TTL_SEC=600" in content
    assert "CLAUDE_HOOK_MAX_PENDING_PERMISSIONS=64" in content
    assert "CLAUDE_JSONL_SYNC_DEBOUNCE_MS=100" in content
    assert "CLAUDE_PERIODIC_RECHECK_MS=500" in content


@pytest.mark.asyncio
async def test_auth_middleware_rejects_callback_query_user() -> None:
    middleware = AuthMiddleware({1})
    callback = DummyCallbackQuery(user_id=2)
    data = {}

    result = await middleware(_passing_handler, callback, data)

    assert result is None
    assert data == {}
    assert callback.answers == ["未授权用户，拒绝访问。"]


@pytest.mark.asyncio
async def test_auth_middleware_allows_callback_query_user() -> None:
    middleware = AuthMiddleware({1})
    callback = DummyCallbackQuery(user_id=1)
    data = {}

    result = await middleware(_passing_handler, callback, data)

    assert result == "ok"
    assert data == {"called": True}
    assert callback.answers == []


@pytest.mark.asyncio
async def test_rate_limit_middleware_limits_callback_query_user() -> None:
    middleware = RateLimitMiddleware(limit=1, window_sec=20)
    callback = DummyCallbackQuery(user_id=1)

    first = await middleware(_passing_handler, callback, {})
    second = await middleware(_passing_handler, callback, {})

    assert first == "ok"
    assert second is None
    assert callback.answers == ["请求过于频繁，请稍后再试。"]
