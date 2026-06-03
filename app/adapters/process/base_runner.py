"""Shared utilities for CLI runners.

Provides common task-lifecycle management and event-finalisation logic used by
both SubprocessRunner and TmuxRunner.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from app.domain.models import CLIEvent, EventType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task registry (dict + lock + cancel tracking)
# ---------------------------------------------------------------------------


@dataclass
class TaskEntry:
    """Wrapper that pairs a task object with its cancel state."""

    task: Any
    cancel_requested: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskRegistry:
    """Thread-safe registry for tracking running tasks and their cancel state."""

    def __init__(self) -> None:
        self._entries: dict[str, TaskEntry] = {}
        self._lock = asyncio.Lock()

    def register(self, task_id: str, task: Any, **metadata: Any) -> None:
        self._entries[task_id] = TaskEntry(task=task, metadata=metadata)

    def unregister(self, task_id: str) -> None:
        self._entries.pop(task_id, None)

    def get_entry(self, task_id: str) -> TaskEntry | None:
        return self._entries.get(task_id)

    def mark_cancelled(self, task_id: str) -> None:
        entry = self._entries.get(task_id)
        if entry is not None:
            entry.cancel_requested = True

    def is_cancelled(self, task_id: str) -> bool:
        entry = self._entries.get(task_id)
        return entry is not None and entry.cancel_requested

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock


# ---------------------------------------------------------------------------
# Event finalisation
# ---------------------------------------------------------------------------

RESULT_TIMEOUT = "timeout"
RESULT_CANCELED = "canceled"
RESULT_EXITED = "exited"
RESULT_FAILED = "failed"


async def yield_terminal_events(
    *,
    task_id: str,
    exit_code: int | None,
    timed_out: bool,
    canceled: bool,
    timeout_sec: int,
    log_extra: dict[str, object],
) -> AsyncGenerator[CLIEvent, None]:
    """Yield the final CLIEvent based on task outcome and log it.

    Each runner calls this at the end of its watch loop so the
    timeout / canceled / exited / failed logic is written once.
    """
    if timed_out:
        logger.warning("task finished", extra={**log_extra, "result": RESULT_TIMEOUT})
        yield CLIEvent(type=EventType.TIMEOUT, task_id=task_id, error=f"任务超时({timeout_sec}s)")
    elif canceled:
        logger.info("task finished", extra={**log_extra, "result": RESULT_CANCELED})
        yield CLIEvent(type=EventType.CANCELED, task_id=task_id, error="任务已取消")
    elif exit_code == 0:
        logger.info("task finished", extra={**log_extra, "result": RESULT_EXITED})
        yield CLIEvent(type=EventType.EXITED, task_id=task_id, exit_code=0)
    else:
        logger.error("task finished", extra={**log_extra, "result": RESULT_FAILED})
        yield CLIEvent(
            type=EventType.FAILED,
            task_id=task_id,
            exit_code=exit_code,
            error=f"进程退出码: {exit_code}",
        )


# ---------------------------------------------------------------------------
# Shared runner base
# ---------------------------------------------------------------------------


class BaseRunner:
    """Common plumbing shared by all runner implementations.

    Subclasses get:
    - ``registry`` for task tracking (dict + lock + cancel state)
    - ``check_empty_argv`` for the universal empty-argv guard
    - ``finalize_and_yield`` for the identical event-finalisation block
    """

    def __init__(self) -> None:
        self.registry = TaskRegistry()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    async def check_empty_argv(argv: list[str], task_id: str) -> AsyncGenerator[CLIEvent, None]:
        """Yield FAILED if *argv* is empty. Caller should ``return`` after iterating."""
        if not argv:
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error="命令参数为空")

    async def finalize_and_yield(
        self,
        *,
        task_id: str,
        exit_code: int | None,
        timed_out: bool,
        canceled: bool,
        timeout_sec: int,
        extra: dict[str, object] | None = None,
    ) -> AsyncGenerator[CLIEvent, None]:
        """Thin wrapper around :func:`yield_terminal_events` for convenience."""
        async for event in yield_terminal_events(
            task_id=task_id,
            exit_code=exit_code,
            timed_out=timed_out,
            canceled=canceled,
            timeout_sec=timeout_sec,
            log_extra=extra or {},
        ):
            yield event
