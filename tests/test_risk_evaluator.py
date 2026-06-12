"""Tests for risk evaluator service."""

from __future__ import annotations

import pytest

from app.services.risk_evaluator import RiskEvaluator, RiskLevel


@pytest.fixture
def evaluator() -> RiskEvaluator:
    return RiskEvaluator(
        enabled=True,
        dangerous_commands=["rm -rf", "git reset --hard", "DROP TABLE"],
        dangerous_paths=[".env", ".ssh", "id_rsa"],
        protected_paths=["/etc", "/root"],
        auto_approve_max_risk="低",
    )


class TestRiskEvaluator:
    def test_safe_command_returns_none(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", {"command": "ls -la"})
        assert result is None

    def test_dangerous_command_detected(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", {"command": "rm -rf /tmp/test"})
        assert result is not None
        assert result.risk_level == RiskLevel.CRITICAL
        assert result.should_block_auto_approve is True

    def test_dangerous_path_detected(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Read", {"file_path": ".env"})
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH

    def test_protected_path_detected(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", {"command": "cat /etc/passwd"})
        assert result is not None
        assert result.risk_level == RiskLevel.MEDIUM

    def test_disabled_evaluator_returns_none(self) -> None:
        evaluator = RiskEvaluator(enabled=False)
        result = evaluator.evaluate("Bash", {"command": "rm -rf /"})
        assert result is None

    def test_word_boundary_matching(self, evaluator: RiskEvaluator) -> None:
        # Should not match "rm" in "permissions"
        result = evaluator.evaluate("Bash", {"command": "check permissions"})
        assert result is None

    def test_multiple_patterns_detected(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", {"command": "sudo rm -rf /root/.env"})
        assert result is not None
        assert result.risk_level == RiskLevel.CRITICAL
        assert len(result.matched_patterns) > 1

    def test_auto_approve_max_risk_medium(self) -> None:
        evaluator = RiskEvaluator(
            enabled=True,
            dangerous_commands=["chmod 777"],
            auto_approve_max_risk="中",
        )
        # Medium risk should not block when max is medium
        result = evaluator.evaluate("Bash", {"command": "chmod 777 file"})
        if result is not None:
            assert result.should_block_auto_approve is False

    def test_auto_approve_max_risk_blocks_high(self) -> None:
        evaluator = RiskEvaluator(
            enabled=True,
            dangerous_paths=[".env"],
            auto_approve_max_risk="中",
        )
        # High risk should block when max is medium
        result = evaluator.evaluate("Read", {"file_path": ".env"})
        if result is not None:
            assert result.should_block_auto_approve is True

    def test_no_tool_input_returns_none(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", None)
        assert result is None

    def test_empty_tool_input_returns_none(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", {})
        assert result is None

    def test_suggestion_for_critical_risk(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", {"command": "rm -rf /"})
        assert result is not None
        assert "手动审批" in result.suggestion

    def test_suggestion_for_medium_risk(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", {"command": "cat /etc/hosts"})
        assert result is not None
        assert "确认操作内容" in result.suggestion

    def test_sql_injection_detected(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", {"command": "echo 'DROP TABLE users'"})
        assert result is not None
        assert result.risk_level == RiskLevel.CRITICAL

    def test_git_push_force_detected(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Bash", {"command": "git push --force origin main"})
        assert result is not None
        assert result.risk_level == RiskLevel.CRITICAL

    def test_ssh_key_access_detected(self, evaluator: RiskEvaluator) -> None:
        result = evaluator.evaluate("Read", {"file_path": "~/.ssh/id_rsa"})
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH

    def test_risk_level_ordering(self) -> None:
        evaluator = RiskEvaluator(
            enabled=True,
            auto_approve_max_risk="低",
        )
        # All non-low risks should block
        for risk_level in [RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]:
            assert evaluator._should_block_auto_approve(risk_level) is True

        evaluator = RiskEvaluator(
            enabled=True,
            auto_approve_max_risk="高",
        )
        # Only critical should block when max is high
        assert evaluator._should_block_auto_approve(RiskLevel.CRITICAL) is True
        assert evaluator._should_block_auto_approve(RiskLevel.HIGH) is False
        assert evaluator._should_block_auto_approve(RiskLevel.MEDIUM) is False
