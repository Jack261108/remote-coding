import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.adapters.storage.file_session_store import FileSessionStore
from app.config.settings import Settings
from app.domain.models import CLIEvent, ExecutionTask
from app.services.session_service import SessionService


def expected_terminal_id(*, user_id: int, workdir: str) -> str:
    return SessionService(store=None)._build_terminal_id(user_id=user_id, workdir=workdir)


class StubAdapter(BaseCLIAdapter):
    provider = "stub"

    def __init__(self, events: list[CLIEvent]) -> None:
        self._events = events
        self.cancel_called = False
        self.last_terminal_key: str | None = None
        self.last_interactive: bool = False
        self.last_claude_session_id: str | None = None

    async def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ) -> AsyncIterator[CLIEvent]:
        self.last_terminal_key = terminal_key
        self.last_interactive = interactive
        self.last_claude_session_id = claude_session_id
        for event in self._events:
            await asyncio.sleep(0)
            yield CLIEvent(
                type=event.type,
                task_id=task.task_id,
                content=event.content,
                exit_code=event.exit_code,
                error=event.error,
            )

    async def cancel(self, task_id: str) -> bool:
        self.cancel_called = True
        return True


class StubFactory:
    def __init__(self, adapter: BaseCLIAdapter) -> None:
        self._adapters = {"claude_code": adapter, "codex": adapter, "gemini": adapter}
        self._closed_terminal_key: str | None = None
        self._ensured_terminal_key: str | None = None
        self._ensured_workdir: str | None = None
        self._ensured_interactive_terminal_key: str | None = None
        self._ensured_interactive_workdir: str | None = None
        self._revealed_terminal_key: str | None = None
        self._interactive_inputs: list[tuple[str, str, str]] = []
        self._user_question_option_actions: list[tuple[str, str, int, bool]] = []
        self._user_question_text_actions: list[tuple[str, str, int, str, bool]] = []
        self._user_question_multi_select_advances: list[tuple[str, str, bool]] = []

    def normalize_provider(self, provider: str) -> str:
        p = provider.strip().lower()
        if p in {"claude", "claude_code", "claude-code"}:
            return "claude_code"
        if p in {"codex", "codex_cli", "codex-cli"}:
            return "codex"
        if p in {"gemini", "gemini_cli", "gemini-cli"}:
            return "gemini"
        raise ValueError("不支持 provider")

    def get(self, provider: str) -> BaseCLIAdapter:
        return self._adapters[self.normalize_provider(provider)]

    def available_providers(self) -> list[str]:
        return ["claude_code", "codex", "gemini"]

    async def close_terminal(self, terminal_key: str) -> tuple[bool, str]:
        self._closed_terminal_key = terminal_key
        return True, ""

    async def ensure_terminal(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        self._ensured_terminal_key = terminal_key
        self._ensured_workdir = workdir
        return True, ""

    async def ensure_claude_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        self._ensured_interactive_terminal_key = terminal_key
        self._ensured_interactive_workdir = workdir
        return True, ""

    async def ensure_claude_resume_session(self, *, terminal_key: str, workdir: str, session_id: str) -> tuple[bool, str]:
        self._ensured_resume_terminal_key = terminal_key
        self._ensured_resume_workdir = workdir
        self._ensured_resume_session_id = session_id
        return True, ""

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        self._revealed_terminal_key = terminal_key
        return True, f"已在桌面打开 Terminal 并附着到 tgcli_{terminal_key}"

    async def send_claude_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        self._interactive_inputs.append((terminal_key, workdir, text))
        return True, ""

    async def select_claude_user_question_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        self._user_question_option_actions.append((terminal_key, workdir, option_index, submit_after))
        return True, ""

    async def answer_claude_user_question_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        self._user_question_text_actions.append((terminal_key, workdir, option_count, text, submit_after))
        return True, ""

    async def advance_claude_user_question_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        self._user_question_multi_select_advances.append((terminal_key, workdir, final_question))
        return True, ""


class DummyHookSocketServer:
    def __init__(self, *, respond_ok: bool = True) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.respond_ok = respond_ok

    async def respond_to_permission(self, *, tool_use_id: str, decision: str, reason: str | None = None) -> bool:
        self.calls.append((tool_use_id, decision, reason))
        return self.respond_ok


def make_settings(tmp_path: Path, *, claude_tmux_mode: bool = False, **overrides: object) -> Settings:
    data = {
        "TG_BOT_TOKEN": "token",
        "TG_ALLOWED_USER_IDS": "1",
        "DEFAULT_PROVIDER": "claude_code",
        "DEFAULT_TIMEOUT_SEC": 10,
        "MAX_CONCURRENT_TASKS": 2,
        "CLAUDE_TMUX_MODE": claude_tmux_mode,
        "CLAUDE_CLI_BIN": "claude",
        "CODEX_CLI_BIN": "codex",
        "GEMINI_CLI_BIN": "gemini",
        "ALLOWED_WORKDIRS": str(tmp_path),
        "TASK_OUTPUT_CHAR_LIMIT": 20,
    }
    data.update(overrides)
    return Settings.model_validate(data)


def make_file_backed_session_service(tmp_path: Path) -> SessionService:
    return SessionService(FileSessionContextStore(FileSessionStore(str(tmp_path))))
