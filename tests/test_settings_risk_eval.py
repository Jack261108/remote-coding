"""Unit tests for ``RISK_EVAL_AUTO_APPROVE_MAX_RISK`` configuration validation.

Ensures the value matches the ``RiskLevel`` enum (低/中/高/极高); invalid values
are rejected at ``Settings`` construction with a readable error instead of
crashing later during bootstrap when ``RiskLevel(value)`` is called.

Cases use ``Settings(_env_file=None)`` with ``monkeypatch.setenv`` so the
on-disk ``.env`` file is ignored and only the explicitly set env vars apply.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_auto_approve_max_risk_defaults_to_low(required_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """When RISK_EVAL_AUTO_APPROVE_MAX_RISK is unset, the default is 低."""
    monkeypatch.delenv("RISK_EVAL_AUTO_APPROVE_MAX_RISK", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.risk_eval_auto_approve_max_risk == "低"


@pytest.mark.parametrize("env_value", ["低", "中", "高", "极高"])
def test_auto_approve_max_risk_accepts_valid_levels(required_settings_env: None, monkeypatch: pytest.MonkeyPatch, env_value: str) -> None:
    """Each RiskLevel value is accepted."""
    monkeypatch.setenv("RISK_EVAL_AUTO_APPROVE_MAX_RISK", env_value)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.risk_eval_auto_approve_max_risk == env_value


@pytest.mark.parametrize("env_value", ["low", "critical", "", "medium", "HIGH"])
def test_auto_approve_max_risk_rejects_invalid_levels(required_settings_env: None, monkeypatch: pytest.MonkeyPatch, env_value: str) -> None:
    """Non-enum values (e.g. English level names) are rejected with a ValidationError."""
    monkeypatch.setenv("RISK_EVAL_AUTO_APPROVE_MAX_RISK", env_value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_auto_approve_max_risk_error_message_lists_valid_values(required_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rejection error names the valid levels so misconfiguration is diagnosable."""
    monkeypatch.setenv("RISK_EVAL_AUTO_APPROVE_MAX_RISK", "low")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    message = str(exc_info.value)
    for level in ("低", "中", "高", "极高"):
        assert level in message
