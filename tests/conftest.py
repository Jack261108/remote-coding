from __future__ import annotations

import pytest


@pytest.fixture
def required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimal env vars needed for ``Settings(_env_file=None)`` to construct."""
    monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TG_ALLOWED_USER_IDS", "1,2")
