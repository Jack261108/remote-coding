"""Unit tests for EXTERNAL_BINDING_IDLE_TTL_HOURS configuration validation.

Covers test plan items T18, T19, T20 from the bugfix test plan
(.kiro/specs/stale-external-binding-cleanup/bugfix.md):

- T18: Default 24h TTL applied when env var is unset.
- T19: EXTERNAL_BINDING_IDLE_TTL_HOURS=1 → 1h TTL.
- T20: EXTERNAL_BINDING_IDLE_TTL_HOURS=0 → startup configuration error.

A bonus negative case verifies that negative values are rejected as well.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimal env vars needed for ``Settings(_env_file=None)`` to construct."""
    monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TG_ALLOWED_USER_IDS", "1,2")


def test_external_binding_idle_ttl_hours_defaults_to_24(monkeypatch: pytest.MonkeyPatch) -> None:
    """T18: When EXTERNAL_BINDING_IDLE_TTL_HOURS is unset, the default of 24 applies."""
    _set_required_env(monkeypatch)
    monkeypatch.delenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.external_binding_idle_ttl_hours == 24


def test_external_binding_idle_ttl_hours_accepts_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """T19: EXTERNAL_BINDING_IDLE_TTL_HOURS=1 is accepted and stored."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", "1")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.external_binding_idle_ttl_hours == 1


def test_external_binding_idle_ttl_hours_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """T20: EXTERNAL_BINDING_IDLE_TTL_HOURS=0 is rejected by the ge=1 validator."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_external_binding_idle_ttl_hours_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bonus: negative values for EXTERNAL_BINDING_IDLE_TTL_HOURS are rejected."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", "-5")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
