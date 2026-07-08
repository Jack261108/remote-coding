from __future__ import annotations

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.cli.claude_code import ClaudeCodeAdapter
from app.adapters.cli.codex_cli import CodexCLIAdapter
from app.adapters.cli.gemini_cli import GeminiCLIAdapter
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.config.settings import Settings
from app.domain.protocols import AdapterCapabilities


class CLIAdapterRegistry:
    """Provider adapter 注册表。"""

    _ALIASES = {
        "claude": "claude_code",
        "claude_code": "claude_code",
        "claude-code": "claude_code",
        "codex": "codex",
        "codex_cli": "codex",
        "codex-cli": "codex",
        "gemini": "gemini",
        "gemini_cli": "gemini",
        "gemini-cli": "gemini",
    }

    def __init__(self, settings: Settings, runner: SubprocessRunner, tmux_runner: TmuxRunner | None = None) -> None:
        self._claude_terminal_enabled = settings.claude_tmux_mode and tmux_runner is not None
        claude_runner: SubprocessRunner | TmuxRunner = tmux_runner if self._claude_terminal_enabled and tmux_runner is not None else runner
        self._adapters: dict[str, BaseCLIAdapter] = {
            "claude_code": ClaudeCodeAdapter(cli_bin=settings.claude_cli_bin, runner=claude_runner),
            "codex": CodexCLIAdapter(cli_bin=settings.codex_cli_bin, runner=runner),
            "gemini": GeminiCLIAdapter(cli_bin=settings.gemini_cli_bin, runner=runner),
        }
        self._capabilities: dict[str, AdapterCapabilities] = {
            "claude_code": AdapterCapabilities(
                persistent_terminal=self._claude_terminal_enabled,
                interactive_input=self._claude_terminal_enabled,
                claude_resume=self._claude_terminal_enabled,
                user_question_tui=self._claude_terminal_enabled,
                session_state=self._claude_terminal_enabled,
            ),
            "codex": AdapterCapabilities(),
            "gemini": AdapterCapabilities(),
        }

    @property
    def claude_terminal_enabled(self) -> bool:
        return self._claude_terminal_enabled

    def normalize_provider(self, provider: str) -> str:
        key = provider.strip().lower()
        normalized = self._ALIASES.get(key)
        if normalized is None:
            raise ValueError(f"不支持 provider: {provider}")
        return normalized

    def get(self, provider: str) -> BaseCLIAdapter:
        normalized = self.normalize_provider(provider)
        return self._adapters[normalized]

    def available_providers(self) -> list[str]:
        return sorted(self._adapters.keys())

    def capabilities(self, provider: str) -> AdapterCapabilities:
        normalized = self.normalize_provider(provider)
        return self._capabilities[normalized]
