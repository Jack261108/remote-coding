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
