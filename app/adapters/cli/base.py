from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from app.domain.models import CLIEvent, ExecutionTask


class BaseCLIAdapter(ABC):
    provider: str

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

    @abstractmethod
    async def cancel(self, task_id: str) -> bool:
        raise NotImplementedError
