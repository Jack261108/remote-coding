"""Session Watcher 基类。"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from app.infra.async_utils import cancel_and_await_tasks

logger = logging.getLogger(__name__)


class BaseSessionWatcher(ABC):
    """会话监听器基类，管理 per-session 的后台任务。"""

    def __init__(self) -> None:
        self._active = True
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def watch(self, *, session_id: str, workdir: str) -> None:
        """启动或跳过已存在的监听任务。"""
        self._active = True
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            return
        self._tasks[session_id] = asyncio.create_task(self._watch_session(session_id=session_id, workdir=workdir))

    def forget(self, *, session_id: str) -> None:
        """移除并取消指定 session 的监听任务。"""
        task = self._tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def stop_all(self) -> None:
        """取消所有监听任务。"""
        self._active = False
        tasks = list(self._tasks.values())
        self._tasks.clear()
        await cancel_and_await_tasks(tasks)

    @abstractmethod
    async def _watch_session(self, *, session_id: str, workdir: str) -> None:
        """子类实现：监听单个 session 的逻辑。"""
        ...
