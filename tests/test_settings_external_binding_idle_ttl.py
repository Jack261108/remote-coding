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


def test_external_binding_idle_ttl_hours_defaults_to_24(required_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """T18: When EXTERNAL_BINDING_IDLE_TTL_HOURS is unset, the default of 24 applies."""
    monkeypatch.delenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.external_binding_idle_ttl_hours == 24


def test_external_binding_idle_ttl_hours_accepts_one(required_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """T19: EXTERNAL_BINDING_IDLE_TTL_HOURS=1 is accepted and stored."""
    monkeypatch.setenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", "1")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.external_binding_idle_ttl_hours == 1


def test_external_binding_idle_ttl_hours_rejects_zero(required_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """T20: EXTERNAL_BINDING_IDLE_TTL_HOURS=0 is rejected by the ge=1 validator."""
    monkeypatch.setenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_external_binding_idle_ttl_hours_rejects_negative(required_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bonus: negative values for EXTERNAL_BINDING_IDLE_TTL_HOURS are rejected."""
    monkeypatch.setenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", "-5")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
