"""Tests for async utility functions.

Covers: cancel_and_await_tasks, cancel_optional_task.
"""

from __future__ import annotations

import asyncio

import pytest

from app.infra.async_utils import cancel_and_await_tasks, cancel_optional_task


class TestCancelAndAwaitTasks:
    @pytest.mark.asyncio
    async def test_cancels_all_tasks(self):
        marker = []

        async def work():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                marker.append("cancelled")
                raise

        tasks = [asyncio.create_task(work()) for _ in range(3)]
        await asyncio.sleep(0)  # let tasks start

        await cancel_and_await_tasks(tasks)

        assert len(marker) == 3
        assert all(t.done() for t in tasks)

    @pytest.mark.asyncio
    async def test_handles_empty_iterable(self):
        await cancel_and_await_tasks([])  # should not raise

    @pytest.mark.asyncio
    async def test_handles_already_done_tasks(self):
        async def quick():
            return 42

        task = asyncio.create_task(quick())
        await asyncio.sleep(0)  # let it finish
        assert task.done()

        await cancel_and_await_tasks([task])  # should not raise


class TestCancelOptionalTask:
    @pytest.mark.asyncio
    async def test_none_is_noop(self):
        await cancel_optional_task(None)  # should not raise

    @pytest.mark.asyncio
    async def test_cancels_running_task(self):
        marker = []

        async def work():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                marker.append("cancelled")
                raise

        task = asyncio.create_task(work())
        await asyncio.sleep(0)

        await cancel_optional_task(task)

        assert len(marker) == 1
        assert task.done()

    @pytest.mark.asyncio
    async def test_already_done_task(self):
        async def quick():
            return "done"

        task = asyncio.create_task(quick())
        await asyncio.sleep(0)

        await cancel_optional_task(task)
        assert task.done()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
