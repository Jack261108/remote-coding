from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Self

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.storage.file_session_context_store import FileSessionContextStore
from app.adapters.storage.file_session_store import FileSessionStore
from app.config.settings import Settings
from app.domain.models import CLIEvent, ExecutionTask
from app.domain.protocols import AdapterCapabilities
from app.domain.session_models import SessionState
from app.services.session_service import SessionService


def expected_terminal_id(*, user_id: int, workdir: str) -> str:
    digest = hashlib.sha1(workdir.encode("utf-8")).hexdigest()[:12]
    return f"user_{user_id}_{digest}"


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
    ) -> AsyncGenerator[CLIEvent, None]:
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
        _ = task_id
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

    def capabilities(self, provider: str) -> AdapterCapabilities:
        _ = self.normalize_provider(provider)
        return AdapterCapabilities(
            persistent_terminal=True,
            interactive_input=True,
            claude_resume=True,
            user_question_tui=True,
            session_state=True,
        )

    @property
    def claude_terminal_runtime(self) -> Self:
        return self

    @property
    def claude_user_question_transport(self) -> Self:
        return self

    @property
    def session_state_reader(self) -> Self:
        return self

    async def close_terminal(self, terminal_key: str) -> tuple[bool, str]:
        self._closed_terminal_key = terminal_key
        return True, ""

    async def ensure_terminal(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        self._ensured_terminal_key = terminal_key
        self._ensured_workdir = workdir
        return True, ""

    async def ensure_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        self._ensured_interactive_terminal_key = terminal_key
        self._ensured_interactive_workdir = workdir
        return True, ""

    async def ensure_claude_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        return await self.ensure_interactive_session(terminal_key=terminal_key, workdir=workdir)

    async def ensure_resume_session(self, *, terminal_key: str, workdir: str, session_id: str) -> tuple[bool, str]:
        self._ensured_resume_terminal_key = terminal_key
        self._ensured_resume_workdir = workdir
        self._ensured_resume_session_id = session_id
        return True, ""

    async def ensure_claude_resume_session(self, *, terminal_key: str, workdir: str, session_id: str) -> tuple[bool, str]:
        return await self.ensure_resume_session(terminal_key=terminal_key, workdir=workdir, session_id=session_id)

    async def reveal_terminal(self, terminal_key: str) -> tuple[bool, str]:
        self._revealed_terminal_key = terminal_key
        return True, f"已在桌面打开 Terminal 并附着到 tgcli_{terminal_key}"

    async def send_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        self._interactive_inputs.append((terminal_key, workdir, text))
        return True, ""

    async def send_claude_interactive_input(self, *, terminal_key: str, workdir: str, text: str) -> tuple[bool, str]:
        return await self.send_interactive_input(terminal_key=terminal_key, workdir=workdir, text=text)

    async def select_option(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_index: int,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        return await self.select_claude_user_question_option(
            terminal_key=terminal_key,
            workdir=workdir,
            option_index=option_index,
            submit_after=submit_after,
        )

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

    async def answer_with_text(
        self,
        *,
        terminal_key: str,
        workdir: str,
        option_count: int,
        text: str,
        submit_after: bool = False,
    ) -> tuple[bool, str]:
        return await self.answer_claude_user_question_with_text(
            terminal_key=terminal_key,
            workdir=workdir,
            option_count=option_count,
            text=text,
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
        self._user_question_text_actions.append((terminal_key, workdir, option_count, text, submit_after))
        return True, ""

    async def advance_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        return await self.advance_claude_user_question_after_multi_select(
            terminal_key=terminal_key,
            workdir=workdir,
            final_question=final_question,
        )

    async def advance_claude_user_question_after_multi_select(
        self,
        *,
        terminal_key: str,
        workdir: str,
        final_question: bool,
    ) -> tuple[bool, str]:
        self._user_question_multi_select_advances.append((terminal_key, workdir, final_question))
        return True, ""

    def get_session_state(self, terminal_key: str) -> SessionState | None:
        _ = terminal_key
        return None

    def get_claude_session_state(self, session_id: str) -> SessionState | None:
        _ = session_id
        return None


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
