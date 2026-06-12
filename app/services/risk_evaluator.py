"""Risk evaluation service for command safety assessment."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class RiskLevel(StrEnum):
    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"
    CRITICAL = "极高"


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    risk_level: RiskLevel
    matched_patterns: list[str]
    suggestion: str
    should_block_auto_approve: bool


class RiskEvaluator:
    """Evaluates risk level of tool calls based on configurable patterns."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        dangerous_commands: list[str] | None = None,
        dangerous_paths: list[str] | None = None,
        protected_paths: list[str] | None = None,
        auto_approve_max_risk: str = "低",
    ) -> None:
        self._enabled = enabled
        self._auto_approve_max_risk = RiskLevel(auto_approve_max_risk)

        # Pre-compile patterns for performance
        self._dangerous_cmd_patterns = [re.compile(re.escape(cmd), re.IGNORECASE) for cmd in (dangerous_commands or [])]
        # Paths may start with dots (like .env), so we don't use word boundary
        self._dangerous_path_patterns = [re.compile(re.escape(path), re.IGNORECASE) for path in (dangerous_paths or [])]
        self._protected_path_patterns = [re.compile(re.escape(path), re.IGNORECASE) for path in (protected_paths or [])]

        # Built-in deletion markers (always checked)
        self._deletion_markers = [
            "rm -rf",
            "rm -r",
            "rm -f",
            "unlink",
            "shred",
            "git reset --hard",
            "git clean -fd",
            "git push --force",
            "git push -f",
            "DROP TABLE",
            "DELETE FROM",
            "TRUNCATE",
            "dd if=",
            "mkfs",
        ]

    def evaluate(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None,
    ) -> RiskAssessment | None:
        """Evaluate risk level for a tool call.

        Returns RiskAssessment if risk detected, None if safe.
        """
        if not self._enabled:
            return None

        # Extract text to analyze
        text_parts = []
        if tool_name:
            text_parts.append(tool_name)
        if tool_input:
            text_parts.extend(self._extract_text_from_input(tool_input))

        raw_text = " ".join(text_parts)
        if not raw_text.strip():
            return None

        text_lower = raw_text.lower()
        matched: list[str] = []

        # Check dangerous commands
        for pattern in self._dangerous_cmd_patterns:
            if pattern.search(text_lower):
                matched.append(pattern.pattern)

        # Check dangerous paths
        for pattern in self._dangerous_path_patterns:
            if pattern.search(text_lower):
                matched.append(pattern.pattern)

        # Check protected paths
        for pattern in self._protected_path_patterns:
            if pattern.search(text_lower):
                matched.append(pattern.pattern)

        # Check deletion markers
        for marker in self._deletion_markers:
            if marker.lower() in text_lower:
                if marker not in matched:
                    matched.append(marker)

        if not matched:
            return None

        # Determine risk level
        risk_level = self._determine_risk_level(matched)

        # Determine if should block auto-approve
        should_block = self._should_block_auto_approve(risk_level)

        suggestion = self._get_suggestion(risk_level)

        return RiskAssessment(
            risk_level=risk_level,
            matched_patterns=matched,
            suggestion=suggestion,
            should_block_auto_approve=should_block,
        )

    def _extract_text_from_input(self, tool_input: dict[str, Any]) -> list[str]:
        """Extract searchable text from tool input."""
        texts = []
        # Common fields that contain command text
        for key in ("command", "content", "file_path", "old_string", "new_string", "pattern"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
        return texts

    def _determine_risk_level(self, matched: list[str]) -> RiskLevel:
        """Determine risk level based on matched patterns."""
        critical_keywords = {
            "rm -rf",
            "sudo rm",
            "dd ",
            "mkfs",
            "git reset --hard",
            "git push --force",
            "drop table",
            "delete from",
            "truncate",
        }
        high_keywords = {
            "sudo",
            "git push",
            "git clean",
            ".env",
            ".ssh",
            "id_rsa",
            "id_ed25519",
            "token",
            "credentials",
            "private_key",
            "secrets",
            ".pem",
            ".key",
        }

        max_risk = RiskLevel.MEDIUM

        for m in matched:
            m_lower = m.lower()
            if any(c in m_lower for c in critical_keywords):
                return RiskLevel.CRITICAL
            if any(h in m_lower for h in high_keywords):
                max_risk = RiskLevel.HIGH

        return max_risk

    def _should_block_auto_approve(self, risk_level: RiskLevel) -> bool:
        """Check if auto-approve should be blocked based on risk level."""
        risk_order = {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
            RiskLevel.CRITICAL: 3,
        }
        return risk_order[risk_level] > risk_order[self._auto_approve_max_risk]

    def _get_suggestion(self, risk_level: RiskLevel) -> str:
        """Get suggestion text based on risk level."""
        if risk_level in (RiskLevel.CRITICAL, RiskLevel.HIGH):
            return "建议手动审批，此操作存在较高风险"
        if risk_level == RiskLevel.MEDIUM:
            return "建议确认操作内容"
        return ""
