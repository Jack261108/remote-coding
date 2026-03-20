from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.adapters.cli.base import BaseCLIAdapter
from app.domain.models import CLIEvent, ExecutionTask


class RunnerProtocol(Protocol):
    async def run(
        self,
        *,
        task_id: str,
        argv: list[str],
        workdir: str,
        timeout_sec: int,
        env: dict[str, str] | None = None,
        terminal_key: str | None = None,
        interactive: bool = False,
    ) -> AsyncIterator[CLIEvent]: ...

    async def ensure_claude_interactive_session(
        self,
        *,
        terminal_key: str,
        workdir: str,
    ) -> tuple[bool, str]: ...

    async def cancel(self, task_id: str) -> bool: ...


class ClaudeCodeAdapter(BaseCLIAdapter):
    provider = "claude_code"

    def __init__(self, cli_bin: str, runner: RunnerProtocol) -> None:
        self._cli_bin = cli_bin
        self._runner = runner

    async def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
    ) -> AsyncIterator[CLIEvent]:
        if interactive:
            argv = [task.prompt]
        else:
            argv = [self._cli_bin, "-p", task.prompt]

        async for event in self._runner.run(
            task_id=task.task_id,
            argv=argv,
            workdir=task.workdir,
            timeout_sec=task.timeout_sec,
            terminal_key=terminal_key,
            interactive=interactive,
        ):
            yield event

    async def ensure_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        return await self._runner.ensure_claude_interactive_session(terminal_key=terminal_key, workdir=workdir)

    async def cancel(self, task_id: str) -> bool:
        return await self._runner.cancel(task_id)
