"""Tests for PeriodicBackgroundTask and BaseSessionWatcher.

Covers: start, stop, is_running, _periodic_loop, _on_error, and
BaseSessionWatcher.watch, forget, stop_all.
"""

from __future__ import annotations

import asyncio

import pytest

from app.infra.periodic_task import PeriodicBackgroundTask
from app.services.session_watcher_base import BaseSessionWatcher

# ---------------------------------------------------------------------------
# PeriodicBackgroundTask
# ---------------------------------------------------------------------------


class ConcretePeriodicTask(PeriodicBackgroundTask):
    def __init__(self, interval: float = 0.05) -> None:
        super().__init__(interval, "TestTask")
        self.execute_count = 0
        self.errors: list[Exception] = []

    async def _execute(self) -> None:
        self.execute_count += 1

    def _on_error(self, exc: Exception) -> None:
        self.errors.append(exc)


class FailingPeriodicTask(ConcretePeriodicTask):
    async def _execute(self) -> None:
        self.execute_count += 1
        raise RuntimeError(f"fail-{self.execute_count}")


class TestPeriodicBackgroundTask:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        task = ConcretePeriodicTask()
        task.start()
        assert task.is_running
        await task.stop()

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        task = ConcretePeriodicTask()
        task.start()
        t1 = task._task
        task.start()
        t2 = task._task
        assert t1 is t2
        await task.stop()

    @pytest.mark.asyncio
    async def test_stop_handles_no_task(self):
        task = ConcretePeriodicTask()
        await task.stop()  # should not raise
        assert not task.is_running

    @pytest.mark.asyncio
    async def test_stop_cancels_running_task(self):
        task = ConcretePeriodicTask()
        task.start()
        assert task.is_running
        await task.stop()
        assert not task.is_running
        assert task._task is None

    @pytest.mark.asyncio
    async def test_executes_periodically(self):
        task = ConcretePeriodicTask(interval=0.03)
        task.start()
        await asyncio.sleep(0.15)
        await task.stop()
        assert task.execute_count >= 2

    @pytest.mark.asyncio
    async def test_error_does_not_stop_loop(self):
        task = FailingPeriodicTask(interval=0.03)
        task.start()
        await asyncio.sleep(0.15)
        await task.stop()
        assert task.execute_count >= 2
        assert len(task.errors) >= 2

    @pytest.mark.asyncio
    async def test_is_running_property(self):
        task = ConcretePeriodicTask()
        assert not task.is_running
        task.start()
        assert task.is_running
        await task.stop()
        assert not task.is_running


# ---------------------------------------------------------------------------
# BaseSessionWatcher
# ---------------------------------------------------------------------------


class ConcreteWatcher(BaseSessionWatcher):
    def __init__(self) -> None:
        super().__init__()
        self.watched: list[tuple[str, str]] = []

    async def _watch_session(self, *, session_id: str, workdir: str) -> None:
        self.watched.append((session_id, workdir))
        try:
            while self._active:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise


class TestBaseSessionWatcher:
    @pytest.mark.asyncio
    async def test_watch_starts_task(self):
        watcher = ConcreteWatcher()
        watcher.watch(session_id="s1", workdir="/tmp")
        await asyncio.sleep(0.02)
        assert "s1" in watcher._tasks
        assert not watcher._tasks["s1"].done()
        await watcher.stop_all()

    @pytest.mark.asyncio
    async def test_watch_skips_existing_active_task(self):
        watcher = ConcreteWatcher()
        watcher.watch(session_id="s1", workdir="/tmp")
        task1 = watcher._tasks["s1"]
        watcher.watch(session_id="s1", workdir="/tmp")
        task2 = watcher._tasks["s1"]
        assert task1 is task2
        await watcher.stop_all()

    @pytest.mark.asyncio
    async def test_forget_cancels_task(self):
        watcher = ConcreteWatcher()
        watcher.watch(session_id="s1", workdir="/tmp")
        await asyncio.sleep(0.02)
        task = watcher._tasks["s1"]
        watcher.forget(session_id="s1")
        assert "s1" not in watcher._tasks
        await asyncio.sleep(0.02)
        assert task.done()

    @pytest.mark.asyncio
    async def test_forget_handles_missing_session(self):
        watcher = ConcreteWatcher()
        watcher.forget(session_id="nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_stop_all_cancels_everything(self):
        watcher = ConcreteWatcher()
        watcher.watch(session_id="s1", workdir="/tmp1")
        watcher.watch(session_id="s2", workdir="/tmp2")
        await asyncio.sleep(0.02)
        await watcher.stop_all()
        assert len(watcher._tasks) == 0
        assert not watcher._active


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
