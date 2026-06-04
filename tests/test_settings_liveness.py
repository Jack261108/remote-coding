"""Unit tests for the external-binding pid-liveness configuration settings.

Covers task 2.2 of the external-binding-pid-liveness spec
(.kiro/specs/external-binding-pid-liveness/tasks.md):

- ``EXTERNAL_BINDING_PID_LIVENESS_ENABLED`` defaults to ``True`` and parses
  truthy/falsy env overrides as a bool.
- ``EXTERNAL_BINDING_IDLE_TTL_HOURS`` keeps its default of ``24`` and rejects
  ``0`` via its ``ge=1`` validation.

These cases use ``Settings(_env_file=None)`` with ``monkeypatch.setenv`` so the
on-disk ``.env`` file is ignored and only the explicitly set env vars apply.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_pid_liveness_enabled_defaults_to_true(required_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """When EXTERNAL_BINDING_PID_LIVENESS_ENABLED is unset, the default is True.

    **Validates: Requirements 10.1, 10.5**
    """
    monkeypatch.delenv("EXTERNAL_BINDING_PID_LIVENESS_ENABLED", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.external_binding_pid_liveness_enabled is True


@pytest.mark.parametrize("env_value", ["false", "0"])
def test_pid_liveness_enabled_env_override_false(required_settings_env: None, monkeypatch: pytest.MonkeyPatch, env_value: str) -> None:
    """A falsy env override ("false" or "0") parses as bool False.

    **Validates: Requirements 10.1, 10.5**
    """
    monkeypatch.setenv("EXTERNAL_BINDING_PID_LIVENESS_ENABLED", env_value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.external_binding_pid_liveness_enabled is False


@pytest.mark.parametrize("env_value", ["true", "1"])
def test_pid_liveness_enabled_env_override_true(required_settings_env: None, monkeypatch: pytest.MonkeyPatch, env_value: str) -> None:
    """A truthy env override ("true" or "1") parses as bool True.

    **Validates: Requirements 10.1, 10.5**
    """
    monkeypatch.setenv("EXTERNAL_BINDING_PID_LIVENESS_ENABLED", env_value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.external_binding_pid_liveness_enabled is True


def test_external_binding_idle_ttl_hours_defaults_to_24(required_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """When EXTERNAL_BINDING_IDLE_TTL_HOURS is unset, the default of 24 applies.

    **Validates: Requirements 10.1, 10.5**
    """
    monkeypatch.delenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.external_binding_idle_ttl_hours == 24


def test_external_binding_idle_ttl_hours_rejects_zero(required_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """EXTERNAL_BINDING_IDLE_TTL_HOURS=0 is rejected by the ge=1 validation.

    **Validates: Requirements 10.1, 10.5**
    """
    monkeypatch.setenv("EXTERNAL_BINDING_IDLE_TTL_HOURS", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
