"""Unit tests for tmux preflight check.

**Validates: Requirements 4.3, 4.5**
"""

from __future__ import annotations

import pytest

from app.infra.tmux_preflight import PreflightResult, tmux_preflight


class TestTmuxPreflight:
    """tmux_preflight pure function unit tests."""

    def test_mode_false_no_tmux_passes(self) -> None:
        """CLAUDE_TMUX_MODE=false without tmux → ok, no tmux error."""
        result = tmux_preflight(False, "tmux", resolver=lambda _: None)
        assert result.ok is True
        assert result.error is None

    def test_mode_true_missing_tmux_fails(self) -> None:
        """CLAUDE_TMUX_MODE=true without tmux → ok=False, error mentions tmux."""
        result = tmux_preflight(True, "tmux", resolver=lambda _: None)
        assert result.ok is False
        assert result.error is not None
        assert "tmux" in result.error.lower()

    def test_mode_true_found_tmux_passes(self) -> None:
        """CLAUDE_TMUX_MODE=true with tmux found → ok."""
        result = tmux_preflight(True, "tmux", resolver=lambda _: "/usr/bin/tmux")
        assert result.ok is True
        assert result.error is None

    def test_preflight_result_is_frozen(self) -> None:
        """PreflightResult is immutable."""
        result = PreflightResult(ok=True, error=None)
        with pytest.raises(AttributeError):
            result.ok = False  # type: ignore[misc]
