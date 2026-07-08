from __future__ import annotations

from app.adapters.process.tmux_runner import TmuxRunner
from app.domain.session_models import SessionState

_TERMINAL_UNAVAILABLE = "CLAUDE_TMUX_MODE 未开启或 tmux 未配置"


class DisabledClaudeTerminalFacade:
    """Claude 持久终端未启用时的兼容实现。"""

    async def close_terminal(self, terminal_key: str) -> tuple[bool, str]:
        _ = terminal_key
        return False, _TERMINAL_UNAVAILABLE

    async def ensure_terminal(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        _ = (terminal_key, workdir)
        return False, _TERMINAL_UNAVAILABLE

    async def ensure_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        _ = (terminal_key, workdir)
        return False, _TERMINAL_UNAVAILABLE

    async def ensure_resume_session(self, *, terminal_key: str, workdir: str, session_id: str) -> tuple[bool, str]:
        _ = (terminal_key, workdir, session_id)
        return False, _TERMINAL_UNAVAILABLE

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        _ = terminal_key
        return False, _TERMINAL_UNAVAILABLE

    async def send_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        _ = (terminal_key, workdir, text)
        return False, _TERMINAL_UNAVAILABLE

    async def select_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        _ = (terminal_key, workdir, option_index, submit_after)
        return False, _TERMINAL_UNAVAILABLE

    async def answer_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        _ = (terminal_key, workdir, option_count, text, submit_after)
        return False, _TERMINAL_UNAVAILABLE

    async def advance_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        _ = (terminal_key, workdir, final_question)
        return False, _TERMINAL_UNAVAILABLE

    def get_session_state(self, terminal_key: str) -> SessionState | None:
        _ = terminal_key
        return None

    def get_claude_session_state(self, session_id: str) -> SessionState | None:
        _ = session_id
        return None


class TmuxClaudeTerminalFacade:
    """把现有 TmuxRunner 暴露为 Claude terminal capability。"""

    def __init__(self, tmux_runner: TmuxRunner) -> None:
        self._tmux_runner = tmux_runner

    async def close_terminal(self, terminal_key: str) -> tuple[bool, str]:
        return await self._tmux_runner.close_terminal(terminal_key)

    async def ensure_terminal(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        return await self._tmux_runner.ensure_terminal(terminal_key=terminal_key, workdir=workdir)

    async def ensure_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        return await self._tmux_runner.ensure_claude_interactive_session(terminal_key=terminal_key, workdir=workdir)

    async def ensure_resume_session(self, *, terminal_key: str, workdir: str, session_id: str) -> tuple[bool, str]:
        return await self._tmux_runner.ensure_claude_resume_session(terminal_key=terminal_key, workdir=workdir, session_id=session_id)

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        return await self._tmux_runner.reveal_terminal(terminal_key)

    async def send_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        return await self._tmux_runner.send_interactive_input(terminal_key=terminal_key, workdir=workdir, text=text)

    async def select_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        return await self._tmux_runner.select_user_question_option(
            terminal_key=terminal_key,
            workdir=workdir,
            option_index=option_index,
            submit_after=submit_after,
        )

    async def answer_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        return await self._tmux_runner.answer_user_question_with_text(
            terminal_key=terminal_key,
            workdir=workdir,
            option_count=option_count,
            text=text,
            submit_after=submit_after,
        )

    async def advance_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        return await self._tmux_runner.advance_user_question_after_multi_select(
            terminal_key=terminal_key,
            workdir=workdir,
            final_question=final_question,
        )

    def get_session_state(self, terminal_key: str) -> SessionState | None:
        return self._tmux_runner.get_session_state(terminal_key)

    def get_claude_session_state(self, session_id: str) -> SessionState | None:
        return self._tmux_runner.get_session_state(session_id)
