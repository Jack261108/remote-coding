from __future__ import annotations

from abc import ABC
from collections.abc import AsyncGenerator
from typing import Any

from app.domain.models import CLIEvent, ExecutionTask


class BaseCLIAdapter(ABC):
    provider: str
    _cli_run_args: list[str] = []  # 子类覆盖，如 ["exec"] 或 ["-p"]

    def __init__(self, cli_bin: str, runner: Any) -> None:
        self._cli_bin = cli_bin
        self._runner = runner

    async def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ) -> AsyncGenerator[CLIEvent, None]:
        argv = [self._cli_bin, *self._cli_run_args, task.prompt]
        async for event in self._runner.run(
            task_id=task.task_id,
            argv=argv,
            workdir=task.workdir,
            timeout_sec=task.timeout_sec,
            terminal_key=terminal_key,
            interactive=interactive,
        ):
            yield event

    async def cancel(self, task_id: str) -> bool:
        return await self._runner.cancel(task_id)
