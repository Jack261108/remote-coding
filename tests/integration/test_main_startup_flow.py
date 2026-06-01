"""Integration tests for main() startup orchestration.

**Validates: Requirements 1.2, 1.6, 4.5, 5.5, 5.7**
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from app.main import main


class TestMainStartupFlow:
    """main() correctly dispatches through the startup pipeline."""

    def test_version_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--version exits with code 0 without starting polling."""
        monkeypatch.setattr(sys, "argv", ["tg-cli-gateway", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    def test_help_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--help exits with code 0 without starting polling."""
        monkeypatch.setattr(sys, "argv", ["tg-cli-gateway", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    def test_missing_required_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Missing required config exits non-0 with error on stderr."""
        monkeypatch.setattr(sys, "argv", ["tg-cli-gateway"])
        monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TG_ALLOWED_USER_IDS", raising=False)
        monkeypatch.chdir(Path(tempfile.mkdtemp()))

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "TG_BOT_TOKEN" in captured.err

    def test_unreadable_env_file_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--env-file pointing to nonexistent path exits non-0."""
        monkeypatch.setattr(sys, "argv", ["tg-cli-gateway", "--env-file", "/nonexistent/.env"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "无法加载" in captured.err

    def test_tmux_mode_true_missing_tmux_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """CLAUDE_TMUX_MODE=true without tmux exits non-0."""
        from app.infra.tmux_preflight import PreflightResult

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("TG_BOT_TOKEN=tok\nTG_ALLOWED_USER_IDS=1\nCLAUDE_TMUX_MODE=true\n")
            env_path = f.name

        try:
            monkeypatch.setattr(sys, "argv", ["tg-cli-gateway", "--env-file", env_path])
            # Mock tmux_preflight to simulate missing tmux
            monkeypatch.setattr(
                "app.infra.tmux_preflight.tmux_preflight",
                lambda *a, **kw: PreflightResult(ok=False, error="tmux not found"),
            )

            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code != 0
            captured = capsys.readouterr()
            assert "tmux" in captured.err.lower()
        finally:
            Path(env_path).unlink(missing_ok=True)
