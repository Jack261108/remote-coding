"""Risk evaluation service for command safety assessment."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# NOTE: 中文值的字母序不等于风险等级的大小序，不要用 min()/max() 或
# 字符串比较来判断风险高低，必须使用 risk_order 映射表。
class RiskLevel(StrEnum):
    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"
    CRITICAL = "极高"


# Risk level classification for known dangerous command/path patterns.
# Keys are lowercase for case-insensitive lookup. Patterns not in this map
# default to MEDIUM when matched.
_RISK_LEVEL_MAP: dict[str, RiskLevel] = {
    # CRITICAL — destructive / disk-overwriting / data-loss commands
    **{
        p: RiskLevel.CRITICAL
        for p in [
            "rm -rf",
            "rm -r",
            "rm -f",
            "sudo rm",
            "dd ",
            "dd if=",
            "mkfs",
            "git reset --hard",
            "git push --force",
            "git push -f",
            "git clean -fd",
            "drop table",
            "delete from",
            "truncate",
            "unlink",
            "shred",
        ]
    },
    # HIGH — sensitive file access / privilege escalation
    **{
        p: RiskLevel.HIGH
        for p in [
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
            "chmod 777",
            "chown root",
        ]
    },
    # MEDIUM — protected system paths
    **{
        p: RiskLevel.MEDIUM
        for p in [
            "/etc",
            "/var",
            "/usr",
            "/root",
        ]
    },
}

_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    risk_level: RiskLevel
    matched_patterns: tuple[str, ...]
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

        # Pre-compile patterns for performance; keep originals for risk-level matching
        cmds = dangerous_commands or []
        self._dangerous_cmd_originals = list(cmds)
        self._dangerous_cmd_patterns = [re.compile(re.escape(cmd), re.IGNORECASE) for cmd in cmds]

        paths = dangerous_paths or []
        self._dangerous_path_originals = list(paths)
        # Paths may start with dots (like .env), so we don't use word boundary
        self._dangerous_path_patterns = [re.compile(re.escape(path), re.IGNORECASE) for path in paths]

        prot = protected_paths or []
        self._protected_path_originals = list(prot)
        self._protected_path_patterns = [re.compile(re.escape(path), re.IGNORECASE) for path in prot]

        # Build risk level map: known patterns get their classification from
        # _RISK_LEVEL_MAP (case-insensitive); unknown custom patterns default to MEDIUM.
        self._risk_level_map: dict[str, RiskLevel] = {}
        for p in (*cmds, *paths, *prot):
            self._risk_level_map[p] = _RISK_LEVEL_MAP.get(p.lower(), RiskLevel.MEDIUM)

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
        for original, pattern in zip(self._dangerous_cmd_originals, self._dangerous_cmd_patterns, strict=True):
            if pattern.search(text_lower):
                matched.append(original)

        # Check dangerous paths
        for original, pattern in zip(self._dangerous_path_originals, self._dangerous_path_patterns, strict=True):
            if pattern.search(text_lower):
                matched.append(original)

        # Check protected paths
        for original, pattern in zip(self._protected_path_originals, self._protected_path_patterns, strict=True):
            if pattern.search(text_lower):
                matched.append(original)

        if not matched:
            return None

        # Determine risk level
        risk_level = self._determine_risk_level(matched)

        # Determine if should block auto-approve
        should_block = self._should_block_auto_approve(risk_level)

        suggestion = self._get_suggestion(risk_level)

        return RiskAssessment(
            risk_level=risk_level,
            matched_patterns=tuple(matched),
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
        """Determine risk level based on matched patterns via explicit mapping."""
        max_risk = RiskLevel.LOW

        for m in matched:
            level = self._risk_level_map.get(m, RiskLevel.MEDIUM)
            if _RISK_ORDER[level] > _RISK_ORDER[max_risk]:
                max_risk = level
            if max_risk is RiskLevel.CRITICAL:
                break

        return max_risk

    def _should_block_auto_approve(self, risk_level: RiskLevel) -> bool:
        """Check if auto-approve should be blocked based on risk level."""
        return _RISK_ORDER[risk_level] > _RISK_ORDER[self._auto_approve_max_risk]

    def _get_suggestion(self, risk_level: RiskLevel) -> str:
        """Get suggestion text based on risk level."""
        if risk_level in (RiskLevel.CRITICAL, RiskLevel.HIGH):
            return "建议手动审批，此操作存在较高风险"
        if risk_level == RiskLevel.MEDIUM:
            return "建议确认操作内容"
        return ""
