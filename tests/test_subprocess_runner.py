import asyncio
import logging
import os
import sys

import pytest

from app.adapters.process.subprocess_runner import SubprocessRunner
from app.domain.models import EventType


@pytest.mark.asyncio
async def test_runner_timeout(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.adapters.process.subprocess_runner")
    runner = SubprocessRunner(kill_grace_sec=0.2)

    events = []
    async for event in runner.run(
        task_id="t1",
        argv=[sys.executable, "-c", "import time; time.sleep(2)"],
        workdir="/tmp",
        timeout_sec=1,
    ):
        events.append(event)

    assert events[0].type == EventType.STARTED
    assert events[-1].type == EventType.TIMEOUT
    assert any(record.message == "subprocess task timeout" for record in caplog.records)
    assert any(record.message == "subprocess task finished" and getattr(record, "result") == "timeout" for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="进程组终止仅在 POSIX 平台验证")
async def test_runner_timeout_terminates_child_process(tmp_path) -> None:
    marker = tmp_path / "child-survived.txt"
    runner = SubprocessRunner(kill_grace_sec=0.2)

    events = []
    async for event in runner.run(
        task_id="t-child-timeout",
        argv=[sys.executable, "-c", _spawn_child_that_writes_later_script(), str(marker)],
        workdir=str(tmp_path),
        timeout_sec=1,
    ):
        events.append(event)

    await asyncio.sleep(1.6)

    assert events[0].type == EventType.STARTED
    assert events[-1].type == EventType.TIMEOUT
    assert not marker.exists()


@pytest.mark.asyncio
async def test_runner_cancel(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.adapters.process.subprocess_runner")
    runner = SubprocessRunner(kill_grace_sec=0.2)

    task = asyncio.create_task(
        _collect_events(
            runner.run(
                task_id="t2",
                argv=[sys.executable, "-c", "import time; time.sleep(5)"],
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
    assert any(record.message == "subprocess task cancel requested" for record in caplog.records)
    assert any(record.message == "subprocess task finished" and getattr(record, "result") == "canceled" for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="进程组终止仅在 POSIX 平台验证")
async def test_runner_cancel_terminates_child_process(tmp_path) -> None:
    marker = tmp_path / "child-survived.txt"
    runner = SubprocessRunner(kill_grace_sec=0.2)

    task = asyncio.create_task(
        _collect_events(
            runner.run(
                task_id="t-child-cancel",
                argv=[sys.executable, "-c", _spawn_child_that_writes_later_script(), str(marker)],
                workdir=str(tmp_path),
                timeout_sec=10,
            )
        )
    )

    await asyncio.sleep(0.3)
    canceled = await runner.cancel("t-child-cancel")
    assert canceled is True

    events = await asyncio.wait_for(task, timeout=2)
    await asyncio.sleep(1.6)

    assert events[0].type == EventType.STARTED
    assert events[-1].type == EventType.CANCELED
    assert not marker.exists()


def _spawn_child_that_writes_later_script() -> str:
    child_code = (
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        "time.sleep(1.5)\n"
        "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')\n"
    )
    return (
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "marker = sys.argv[1]\n"
        f"child_code = {child_code!r}\n"
        "subprocess.Popen([sys.executable, '-c', child_code, marker])\n"
        "time.sleep(10)\n"
    )


async def _collect_events(stream):
    result = []
    async for event in stream:
        result.append(event)
    return result
