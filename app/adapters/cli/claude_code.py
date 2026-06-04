from __future__ import annotations

from collections.abc import AsyncGenerator

from app.adapters.cli.base import BaseCLIAdapter
from app.adapters.process.tmux_runner import TmuxRunner
from app.domain.models import CLIEvent, ExecutionTask


class ClaudeCodeAdapter(BaseCLIAdapter):
    provider = "claude_code"

    async def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ) -> AsyncGenerator[CLIEvent, None]:
        if interactive:
            argv = [task.prompt]
        else:
            argv = [self._cli_bin, "-p", task.prompt]
            if task.extra_cli_args:
                argv.extend(task.extra_cli_args)

        async for event in self._runner.run(
            task_id=task.task_id,
            argv=argv,
            workdir=task.workdir,
            timeout_sec=task.timeout_sec,
            terminal_key=terminal_key,
            interactive=interactive,
            claude_session_id=claude_session_id or task.claude_session_id,
        ):
            yield event

    async def ensure_interactive_session(self, *, terminal_key: str, workdir: str) -> tuple[bool, str]:
        if isinstance(self._runner, TmuxRunner):
            return await self._runner.ensure_claude_interactive_session(terminal_key=terminal_key, workdir=workdir)
        return False, "tmux 模式未启用"
