from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from app.domain.models import CLIEvent, ExecutionTask


class BaseCLIAdapter(ABC):
    provider: str

    def __init__(self, cli_bin: str, runner: Any) -> None:
        self._cli_bin = cli_bin
        self._runner = runner

    @abstractmethod
    async def run(
        self,
        task: ExecutionTask,
        *,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ) -> AsyncGenerator[CLIEvent, None]:
        raise NotImplementedError
        yield  # pragma: no cover — makes this an async generator

    async def cancel(self, task_id: str) -> bool:
        return await self._runner.cancel(task_id)
