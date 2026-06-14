"""Tests for PeriodicJanitor.

Covers: register, start, stop, run (single scheduling pass), _run (internal loop).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services.periodic_janitor import PeriodicJanitor


class TestPeriodicJanitorRegister:
    def test_register_stores_job(self):
        janitor = PeriodicJanitor()
        cb = AsyncMock()
        janitor.register("test", 10.0, cb)
        assert "test" in janitor._jobs
        interval, callback = janitor._jobs["test"]
        assert interval == 10.0
        assert callback is cb

    def test_register_rejects_non_positive_interval(self):
        janitor = PeriodicJanitor()
        with pytest.raises(ValueError, match="positive"):
            janitor.register("bad", 0, AsyncMock())
        with pytest.raises(ValueError, match="positive"):
            janitor.register("bad", -1.0, AsyncMock())


class TestPeriodicJanitorRun:
    @pytest.mark.asyncio
    async def test_run_executes_due_jobs(self):
        janitor = PeriodicJanitor()
        calls = []

        async def job():
            calls.append("called")

        janitor.register("job1", 0.001, job)
        janitor._last_run["job1"] = 0

        await janitor.run()
        assert calls == ["called"]

    @pytest.mark.asyncio
    async def test_run_skips_not_due_jobs(self):
        janitor = PeriodicJanitor()
        calls = []

        async def job():
            calls.append("called")

        janitor.register("job1", 9999.0, job)
        now = asyncio.get_running_loop().time()
        janitor._last_run["job1"] = now

        await janitor.run()
        assert calls == []

    @pytest.mark.asyncio
    async def test_run_continues_after_job_failure(self):
        janitor = PeriodicJanitor()
        calls = []

        async def failing_job():
            raise RuntimeError("boom")

        async def good_job():
            calls.append("good")

        janitor.register("failing", 0.001, failing_job)
        janitor.register("good", 0.001, good_job)
        janitor._last_run["failing"] = 0
        janitor._last_run["good"] = 0

        await janitor.run()
        assert calls == ["good"]

    @pytest.mark.asyncio
    async def test_run_updates_last_run_time(self):
        janitor = PeriodicJanitor()

        async def job():
            pass

        janitor.register("job", 0.001, job)
        janitor._last_run["job"] = 0

        before = asyncio.get_running_loop().time()
        await janitor.run()
        after = asyncio.get_running_loop().time()

        assert janitor._last_run["job"] >= before
        assert janitor._last_run["job"] <= after


class TestPeriodicJanitorStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        janitor = PeriodicJanitor()

        async def job():
            pass

        janitor.register("job", 60.0, job)
        await janitor.start()
        assert janitor._task is not None
        assert not janitor._task.done()

        await janitor.stop()

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        janitor = PeriodicJanitor()

        async def job():
            pass

        janitor.register("job", 60.0, job)
        await janitor.start()
        task1 = janitor._task
        await janitor.start()
        task2 = janitor._task
        assert task1 is task2

        await janitor.stop()

    @pytest.mark.asyncio
    async def test_stop_handles_no_task(self):
        janitor = PeriodicJanitor()
        await janitor.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_stop_cancels_running_task(self):
        janitor = PeriodicJanitor()

        async def job():
            pass

        janitor.register("job", 60.0, job)
        await janitor.start()
        task = janitor._task
        await janitor.stop()
        assert janitor._task is None
        assert task.done()

    @pytest.mark.asyncio
    async def test_loop_executes_jobs_periodically(self):
        janitor = PeriodicJanitor()
        calls = []

        async def job():
            calls.append(asyncio.get_running_loop().time())

        janitor.register("job", 0.05, job)
        await janitor.start()
        await asyncio.sleep(0.2)
        await janitor.stop()

        assert len(calls) >= 2


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
