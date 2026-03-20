import asyncio

import pytest

from app.adapters.process.subprocess_runner import SubprocessRunner
from app.domain.models import EventType


@pytest.mark.asyncio
async def test_runner_timeout() -> None:
    runner = SubprocessRunner(kill_grace_sec=0.2)

    events = []
    async for event in runner.run(
        task_id="t1",
        argv=["python3", "-c", "import time; time.sleep(2)"],
        workdir="/tmp",
        timeout_sec=1,
    ):
        events.append(event)

    assert events[0].type == EventType.STARTED
    assert events[-1].type == EventType.TIMEOUT


@pytest.mark.asyncio
async def test_runner_cancel() -> None:
    runner = SubprocessRunner(kill_grace_sec=0.2)

    task = asyncio.create_task(
        _collect_events(
            runner.run(
                task_id="t2",
                argv=["python3", "-c", "import time; time.sleep(5)"],
                workdir="/tmp",
                timeout_sec=10,
            )
        )
    )

    await asyncio.sleep(0.3)
    canceled = await runner.cancel("t2")
    assert canceled is True

    events = await task
    assert events[0].type == EventType.STARTED
    assert events[-1].type == EventType.CANCELED


async def _collect_events(stream):
    result = []
    async for event in stream:
        result.append(event)
    return result
