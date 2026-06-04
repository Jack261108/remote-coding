from __future__ import annotations

from collections.abc import AsyncGenerator

from app.adapters.cli.base import BaseCLIAdapter
from app.domain.models import CLIEvent, ExecutionTask


class CodexCLIAdapter(BaseCLIAdapter):
    provider = "codex"

    async def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ) -> AsyncGenerator[CLIEvent, None]:
        argv = [self._cli_bin, "exec", task.prompt]
        async for event in self._runner.run(
            task_id=task.task_id,
            argv=argv,
            workdir=task.workdir,
            timeout_sec=task.timeout_sec,
            terminal_key=terminal_key,
            interactive=interactive,
        ):
            yield event
