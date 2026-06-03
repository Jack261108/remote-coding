"""异步工具函数。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable


async def cancel_and_await_tasks(tasks: Iterable[asyncio.Task[object]]) -> None:
    """取消所有任务并等待它们完成。"""
    task_list = list(tasks)
    for task in task_list:
        task.cancel()
    for task in task_list:
        try:
            await task
        except asyncio.CancelledError:
            pass


async def cancel_optional_task(task: asyncio.Task[object] | None) -> None:
    """取消单个可选任务并等待完成。"""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
