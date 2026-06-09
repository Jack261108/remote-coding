from __future__ import annotations

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.cli.claude_code import ClaudeCodeAdapter
from app.adapters.cli.codex_cli import CodexCLIAdapter
from app.adapters.cli.gemini_cli import GeminiCLIAdapter
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.config.settings import Settings
from app.domain.session_models import SessionState


class CLIAdapterFactory:
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
        self._claude_tmux_enabled = settings.claude_tmux_mode and tmux_runner is not None
        self._tmux_runner = tmux_runner

        claude_runner: SubprocessRunner | TmuxRunner = tmux_runner if self._claude_tmux_enabled and tmux_runner is not None else runner
        self._adapters: dict[str, BaseCLIAdapter] = {
            "claude_code": ClaudeCodeAdapter(cli_bin=settings.claude_cli_bin, runner=claude_runner),
            "codex": CodexCLIAdapter(cli_bin=settings.codex_cli_bin, runner=runner),
            "gemini": GeminiCLIAdapter(cli_bin=settings.gemini_cli_bin, runner=runner),
        }

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

    def _require_tmux(self) -> TmuxRunner | None:
        """返回可用的 TmuxRunner，tmux 未启用或不可用时返回 None。"""
        if self._claude_tmux_enabled and self._tmux_runner is not None:
            return self._tmux_runner
        return None

    async def close_terminal(self, terminal_key: str) -> tuple[bool, str]:
        tmux = self._require_tmux()
        if not tmux:
            return False, "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"
        return await tmux.close_terminal(terminal_key)

    async def ensure_terminal(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        tmux = self._require_tmux()
        if not tmux:
            return False, "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"
        return await tmux.ensure_terminal(terminal_key=terminal_key, workdir=workdir)

    async def ensure_claude_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        tmux = self._require_tmux()
        if not tmux:
            return False, "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"

        claude_adapter = self._adapters.get("claude_code")
        if isinstance(claude_adapter, ClaudeCodeAdapter):
            return await claude_adapter.ensure_interactive_session(terminal_key=terminal_key, workdir=workdir)

        return False, "claude adapter 不可用"

    async def ensure_claude_resume_session(self, *, terminal_key: str, workdir: str, session_id: str) -> tuple[bool, str]:
        tmux = self._require_tmux()
        if not tmux:
            return False, "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"
        return await tmux.ensure_claude_resume_session(terminal_key=terminal_key, workdir=workdir, session_id=session_id)

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        tmux = self._require_tmux()
        if not tmux:
            return False, "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"
        return await tmux.reveal_terminal(terminal_key)

    async def send_claude_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        tmux = self._require_tmux()
        if not tmux:
            return False, "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"
        return await tmux.send_interactive_input(terminal_key=terminal_key, workdir=workdir, text=text)

    async def select_claude_user_question_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        tmux = self._require_tmux()
        if not tmux:
            return False, "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"
        return await tmux.select_user_question_option(
            terminal_key=terminal_key,
            workdir=workdir,
            option_index=option_index,
            submit_after=submit_after,
        )

    async def answer_claude_user_question_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        tmux = self._require_tmux()
        if not tmux:
            return False, "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"
        return await tmux.answer_user_question_with_text(
            terminal_key=terminal_key,
            workdir=workdir,
            option_count=option_count,
            text=text,
            submit_after=submit_after,
        )

    async def advance_claude_user_question_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        tmux = self._require_tmux()
        if not tmux:
            return False, "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"
        return await tmux.advance_user_question_after_multi_select(
            terminal_key=terminal_key,
            workdir=workdir,
            final_question=final_question,
        )

    def get_session_state(self, terminal_key: str) -> SessionState | None:
        tmux = self._require_tmux()
        if not tmux:
            return None
        return tmux.get_session_state(terminal_key)

    def get_claude_session_state(self, session_id: str) -> SessionState | None:
        tmux = self._require_tmux()
        if not tmux:
            return None
        return tmux.get_session_state(session_id)
