import asyncio
import logging
import os
import sys

import pytest

from app.adapters.process.subprocess_runner import _STREAM_CHUNK_SIZE, SubprocessRunner
from app.domain.models import CLIEvent, EventType


@pytest.mark.asyncio
async def test_runner_timeout(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.adapters.process.subprocess_runner")
    caplog.set_level(logging.INFO, logger="app.adapters.process.base_runner")
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
    assert any(record.message == "task finished" and record.result == "timeout" for record in caplog.records)


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
    caplog.set_level(logging.INFO, logger="app.adapters.process.base_runner")
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
    assert any(record.message == "task finished" and record.result == "canceled" for record in caplog.records)


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


@pytest.mark.asyncio
async def test_runner_handles_long_output_lines() -> None:
    runner = SubprocessRunner(kill_grace_sec=0.2)
    line_len = 10 * 1024 * 1024 + 1

    events = []
    async for event in runner.run(
        task_id="t-long-line",
        argv=[sys.executable, "-c", f"print('x' * {line_len})"],
        workdir="/tmp",
        timeout_sec=10,
    ):
        events.append(event)

    assert events[-1].type == EventType.EXITED
    stdout = "".join(e.content or "" for e in events if e.type == EventType.STDOUT)
    assert stdout.strip() == "x" * line_len


@pytest.mark.asyncio
async def test_pump_stream_preserves_utf8_character_across_chunk_boundary() -> None:
    runner = SubprocessRunner()
    text = "x" * (_STREAM_CHUNK_SIZE - 1) + "中\n"

    events = await _pump_bytes(runner, text.encode())

    assert [event.content for event in events] == [text]


@pytest.mark.asyncio
async def test_pump_stream_preserves_line_boundaries() -> None:
    runner = SubprocessRunner()
    long_line = "x" * (_STREAM_CHUNK_SIZE + 3)

    events = await _pump_bytes(runner, f"first\n{long_line}\nlast".encode(), EventType.STDERR)

    assert [event.content for event in events] == ["first\n", f"{long_line}\n", "last"]
    assert all(event.type == EventType.STDERR for event in events)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="进程组终止仅在 POSIX 平台验证")
async def test_runner_terminates_process_when_stream_abandoned(tmp_path) -> None:
    marker = tmp_path / "child-survived.txt"
    runner = SubprocessRunner(kill_grace_sec=0.2)

    script = "import pathlib, sys, time\ntime.sleep(1.5)\npathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')\n"
    stream = runner.run(
        task_id="t-abandon",
        argv=[sys.executable, "-c", script, str(marker)],
        workdir=str(tmp_path),
        timeout_sec=10,
    )

    async for event in stream:
        assert event.type == EventType.STARTED
        break
    await stream.aclose()

    await asyncio.sleep(1.6)
    assert not marker.exists()
    assert runner.registry.get_entry("t-abandon") is None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="进程组终止仅在 POSIX 平台验证")
async def test_runner_abandonment_kills_sigterm_ignoring_process_group_child(tmp_path) -> None:
    ready = tmp_path / "child-ready.txt"
    marker = tmp_path / "child-survived.txt"
    runner = SubprocessRunner(kill_grace_sec=0.1)
    child_code = (
        "import pathlib\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
        "time.sleep(1)\n"
        "pathlib.Path(sys.argv[2]).write_text('survived', encoding='utf-8')\n"
    )
    parent_code = (
        "import pathlib\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])\n"
        "while not pathlib.Path(sys.argv[2]).exists():\n"
        "    time.sleep(0.01)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    stream = runner.run(
        task_id="t-abandon-group",
        argv=[sys.executable, "-c", parent_code, child_code, str(ready), str(marker)],
        workdir=str(tmp_path),
        timeout_sec=60,
    )

    assert (await anext(stream)).type == EventType.STARTED
    while True:
        event = await anext(stream)
        if event.type == EventType.STDOUT and event.content == "ready\n":
            break
    await stream.aclose()

    await asyncio.sleep(1.2)
    assert not marker.exists()
    assert runner.registry.get_entry("t-abandon-group") is None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="进程组终止仅在 POSIX 平台验证")
async def test_runner_abandonment_kills_group_after_leader_exits(tmp_path) -> None:
    ready = tmp_path / "child-ready.txt"
    marker = tmp_path / "child-survived.txt"
    runner = SubprocessRunner(kill_grace_sec=0.1)
    child_code = (
        "import pathlib\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
        "time.sleep(1.5)\n"
        "pathlib.Path(sys.argv[2]).write_text('survived', encoding='utf-8')\n"
    )
    parent_code = (
        "import pathlib\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])\n"
        "while not pathlib.Path(sys.argv[2]).exists():\n"
        "    time.sleep(0.01)\n"
    )
    stream = runner.run(
        task_id="t-abandon-exited-leader",
        argv=[sys.executable, "-c", parent_code, child_code, str(ready), str(marker)],
        workdir=str(tmp_path),
        timeout_sec=60,
    )

    assert (await anext(stream)).type == EventType.STARTED
    for _ in range(50):
        entry = runner.registry.get_entry("t-abandon-exited-leader")
        if entry is not None and entry.task.returncode is not None:
            break
        await asyncio.sleep(0.01)
    entry = runner.registry.get_entry("t-abandon-exited-leader")
    assert entry is not None and entry.task.returncode == 0

    await stream.aclose()
    await asyncio.sleep(1.6)

    assert not marker.exists()
    assert runner.registry.get_entry("t-abandon-exited-leader") is None


async def _pump_bytes(
    runner: SubprocessRunner,
    payload: bytes,
    event_type: EventType = EventType.STDOUT,
) -> list[CLIEvent]:
    stream = asyncio.StreamReader()
    stream.feed_data(payload)
    stream.feed_eof()
    queue: asyncio.Queue[CLIEvent | None] = asyncio.Queue()

    await runner._pump_stream(task_id="t-pump", stream=stream, event_type=event_type, queue=queue)

    events: list[CLIEvent] = []
    while True:
        event = await queue.get()
        if event is None:
            return events
        events.append(event)


def _spawn_child_that_writes_later_script() -> str:
    child_code = (
        "import pathlib\nimport sys\nimport time\ntime.sleep(1.5)\npathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')\n"
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
