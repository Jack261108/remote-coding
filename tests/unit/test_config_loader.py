"""Unit tests for configuration loading.

**Validates: Requirements 5.1, 5.4, 5.7**
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.config.loader import (
    EnvFileAction,
    StartupError,
    classify_env_file,
    load_settings,
    missing_required_fields,
)


class TestMissingRequiredFields:
    """missing_required_fields identifies items missing in both sources."""

    def test_both_present_returns_empty(self) -> None:
        env = {"TG_BOT_TOKEN": "tok", "TG_ALLOWED_USER_IDS": "1,2"}
        dotenv = {"TG_BOT_TOKEN": "tok2", "TG_ALLOWED_USER_IDS": "3"}
        assert missing_required_fields(env, dotenv) == []

    def test_missing_in_both(self) -> None:
        assert missing_required_fields({}, {}) == ["TG_BOT_TOKEN", "TG_ALLOWED_USER_IDS"]

    def test_present_in_env_only_not_missing(self) -> None:
        env = {"TG_BOT_TOKEN": "tok"}
        assert missing_required_fields(env, {}) == ["TG_ALLOWED_USER_IDS"]

    def test_blank_value_counts_as_missing(self) -> None:
        env = {"TG_BOT_TOKEN": "   ", "TG_ALLOWED_USER_IDS": ""}
        assert missing_required_fields(env, {}) == ["TG_BOT_TOKEN", "TG_ALLOWED_USER_IDS"]


class TestClassifyEnvFile:
    """classify_env_file returns correct action for each scenario."""

    def test_explicit_unreadable(self) -> None:
        assert classify_env_file("/no/such/file", False, default_env_exists=False) == EnvFileAction.ERROR_UNREADABLE

    def test_explicit_readable(self) -> None:
        assert classify_env_file("/some/file", True, default_env_exists=False) == EnvFileAction.LOAD

    def test_none_no_default(self) -> None:
        assert classify_env_file(None, False, default_env_exists=False) == EnvFileAction.FALLBACK

    def test_none_with_default(self) -> None:
        assert classify_env_file(None, False, default_env_exists=True) == EnvFileAction.LOAD


class TestLoadSettings:
    """load_settings translates failures into StartupError."""

    def test_missing_required_raises_startup_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When required env vars are absent, load_settings raises StartupError."""
        # Clear required env vars
        monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TG_ALLOWED_USER_IDS", raising=False)
        monkeypatch.chdir(Path(tempfile.mkdtemp()))

        with pytest.raises(StartupError) as exc_info:
            load_settings(None)
        assert "TG_BOT_TOKEN" in str(exc_info.value)

    def test_unreadable_env_file_raises_startup_error(self) -> None:
        """Explicit --env-file pointing to non-existent path raises StartupError."""
        with pytest.raises(StartupError) as exc_info:
            load_settings("/nonexistent/path/.env")
        assert "无法加载" in str(exc_info.value)

    def test_valid_env_file_loads_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid --env-file with required vars loads Settings successfully."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("TG_BOT_TOKEN=test_token\nTG_ALLOWED_USER_IDS=123\n")
            env_path = f.name

        try:
            monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
            monkeypatch.delenv("TG_ALLOWED_USER_IDS", raising=False)
            settings = load_settings(env_path)
            assert settings.tg_bot_token == "test_token"
        finally:
            Path(env_path).unlink(missing_ok=True)
