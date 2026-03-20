from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

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
    ) -> AsyncIterator[CLIEvent]:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, task_id: str) -> bool:
        raise NotImplementedError
