"""Domain-layer protocols (interfaces) for cross-layer dependencies.

Adapters should depend on these protocols instead of concrete service classes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.domain.models import CLIEvent, ExecutionTask
    from app.domain.session_models import SessionEvent, SessionPhase, SessionState


@dataclass(frozen=True)
class AdapterCapabilities:
    """声明 CLI adapter/runtime 可用能力的轻量视图。"""

    run_task: bool = True
    cancel_task: bool = True
    persistent_terminal: bool = False
    interactive_input: bool = False
    claude_resume: bool = False
    user_question_tui: bool = False
    session_state: bool = False


@runtime_checkable
class CLIAdapterProtocol(Protocol):
    """一次性或交互式任务执行 adapter。"""

    provider: str

    def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ) -> AsyncGenerator[CLIEvent, None]: ...

    async def cancel(self, task_id: str) -> bool: ...


@runtime_checkable
class CLIAdapterRegistryProtocol(Protocol):
    """Provider 归一化与 adapter 查询接口。"""

    def normalize_provider(self, provider: str) -> str: ...

    def get(self, provider: str) -> CLIAdapterProtocol: ...

    def available_providers(self) -> list[str]: ...

    def capabilities(self, provider: str) -> AdapterCapabilities: ...


@runtime_checkable
class ClaudeTerminalRuntimeProtocol(Protocol):
    """Claude 持久终端生命周期和文本输入能力。"""

    async def close_terminal(self, terminal_key: str) -> tuple[bool, str]: ...

    async def ensure_terminal(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]: ...

    async def ensure_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]: ...

    async def ensure_resume_session(self, *, terminal_key: str, workdir: str, session_id: str) -> tuple[bool, str]: ...

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]: ...

    async def send_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]: ...


@runtime_checkable
class ClaudeUserQuestionTransportProtocol(Protocol):
    """Claude AskUserQuestion 的终端/TUI 操作能力。"""

    async def select_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]: ...

    async def answer_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]: ...

    async def advance_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]: ...


@runtime_checkable
class SessionStateReaderProtocol(Protocol):
    """读取 adapter/runtime 内部 structured session state 的兼容接口。"""

    def get_session_state(self, terminal_key: str) -> SessionState | None: ...

    def get_claude_session_state(self, session_id: str) -> SessionState | None: ...


@runtime_checkable
class CLIInfrastructureProtocol(CLIAdapterRegistryProtocol, Protocol):
    """组合 registry 与 Claude runtime capability 的兼容入口。"""

    @property
    def claude_terminal_runtime(self) -> ClaudeTerminalRuntimeProtocol: ...

    @property
    def claude_user_question_transport(self) -> ClaudeUserQuestionTransportProtocol | None: ...

    @property
    def session_state_reader(self) -> SessionStateReaderProtocol: ...


@runtime_checkable
class SessionStoreProtocol(Protocol):
    """Interface for session store operations used by adapters."""

    def get_or_create(
        self,
        *,
        session_id: str,
        user_id: int | None = None,
        provider: str = "claude_code",
        workdir: str = ".",
        terminal_id: str | None = None,
        claude_session_id: str | None = None,
    ) -> SessionState: ...

    def get(self, session_id: str) -> SessionState | None: ...

    def process(self, event: SessionEvent) -> SessionState: ...

    def mark_interactive_turn_processing(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> SessionState | None: ...

    def latest_completed_assistant_turn_id(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> str | None: ...

    def resolve_interactive_session_id(
        self,
        *,
        terminal_id: str | None,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
        require_claude_session: bool = False,
    ) -> str | None: ...

    def get_interactive_state(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
        require_claude_session: bool = False,
    ) -> SessionState | None: ...

    def interactive_completion_phase(
        self,
        *,
        terminal_id: str | None,
        workdir: str,
        claude_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> SessionPhase | None: ...
