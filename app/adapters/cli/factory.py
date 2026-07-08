from __future__ import annotations

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.cli.registry import CLIAdapterRegistry
from app.adapters.process.claude_terminal_facade import DisabledClaudeTerminalFacade, TmuxClaudeTerminalFacade
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.adapters.process.tmux_runner import TmuxRunner
from app.config.settings import Settings
from app.domain.protocols import AdapterCapabilities
from app.domain.session_models import SessionState


class CLIAdapterFactory:
    """兼容旧调用方的 CLI adapter 聚合入口。"""

    def __init__(self, settings: Settings, runner: SubprocessRunner, tmux_runner: TmuxRunner | None = None) -> None:
        self._registry = CLIAdapterRegistry(settings=settings, runner=runner, tmux_runner=tmux_runner)
        self._terminal_facade = (
            TmuxClaudeTerminalFacade(tmux_runner)
            if self._registry.claude_terminal_enabled and tmux_runner is not None
            else DisabledClaudeTerminalFacade()
        )

    @property
    def claude_terminal_runtime(self) -> TmuxClaudeTerminalFacade | DisabledClaudeTerminalFacade:
        return self._terminal_facade

    @property
    def claude_user_question_transport(self) -> TmuxClaudeTerminalFacade | DisabledClaudeTerminalFacade:
        return self._terminal_facade

    @property
    def session_state_reader(self) -> TmuxClaudeTerminalFacade | DisabledClaudeTerminalFacade:
        return self._terminal_facade

    def normalize_provider(self, provider: str) -> str:
        return self._registry.normalize_provider(provider)

    def get(self, provider: str) -> BaseCLIAdapter:
        return self._registry.get(provider)

    def available_providers(self) -> list[str]:
        return self._registry.available_providers()

    def capabilities(self, provider: str) -> AdapterCapabilities:
        return self._registry.capabilities(provider)

    async def close_terminal(self, terminal_key: str) -> tuple[bool, str]:
        return await self._terminal_facade.close_terminal(terminal_key)

    async def ensure_terminal(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        return await self._terminal_facade.ensure_terminal(terminal_key=terminal_key, workdir=workdir)

    async def ensure_claude_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        return await self._terminal_facade.ensure_interactive_session(terminal_key=terminal_key, workdir=workdir)

    async def ensure_claude_resume_session(self, *, terminal_key: str, workdir: str, session_id: str) -> tuple[bool, str]:
        return await self._terminal_facade.ensure_resume_session(terminal_key=terminal_key, workdir=workdir, session_id=session_id)

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        return await self._terminal_facade.reveal_terminal(terminal_key)

    async def send_claude_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        return await self._terminal_facade.send_interactive_input(terminal_key=terminal_key, workdir=workdir, text=text)

    async def select_claude_user_question_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        return await self._terminal_facade.select_option(
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
        return await self._terminal_facade.answer_with_text(
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
        return await self._terminal_facade.advance_after_multi_select(
            terminal_key=terminal_key,
            workdir=workdir,
            final_question=final_question,
        )

    def get_session_state(self, terminal_key: str) -> SessionState | None:
        return self._terminal_facade.get_session_state(terminal_key)

    def get_claude_session_state(self, session_id: str) -> SessionState | None:
        return self._terminal_facade.get_claude_session_state(session_id)
