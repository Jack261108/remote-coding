from app.bot.middleware.auth import AuthMiddleware
from app.config.settings import Settings


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
            "CLAUDE_JSONL_SYNC_DEBOUNCE_MS": 250,
            "CODEX_CLI_BIN": "codex",
            "GEMINI_CLI_BIN": "gemini",
            "ALLOWED_WORKDIRS": "/tmp",
        }
    )

    assert settings.claude_config_dir == "~/.config/claude"
    assert settings.claude_hook_socket_path == "/tmp/remote-coding.sock"
    assert settings.claude_install_hooks is True
    assert settings.claude_jsonl_sync_debounce_ms == 250
