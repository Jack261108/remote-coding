from __future__ import annotations

from collections.abc import AsyncIterator

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.process.subprocess_runner import SubprocessRunner
from app.domain.models import CLIEvent, ExecutionTask


class CodexCLIAdapter(BaseCLIAdapter):
    provider = "codex"

    def __init__(self, cli_bin: str, runner: SubprocessRunner) -> None:
        self._cli_bin = cli_bin
        self._runner = runner

    async def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
    ) -> AsyncIterator[CLIEvent]:
        argv = [self._cli_bin, "exec", task.prompt]
        async for event in self._runner.run(
            task_id=task.task_id,
            argv=argv,
            workdir=task.workdir,
            timeout_sec=task.timeout_sec,
            terminal_key=terminal_key,
            interactive=interactive,
            provider=self.provider,
        ):
            yield event

    async def cancel(self, task_id: str) -> bool:
        return await self._runner.cancel(task_id)
