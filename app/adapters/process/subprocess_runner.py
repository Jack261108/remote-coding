from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import AsyncGenerator
from typing import Any

from app.adapters.process.base_runner import BaseRunner, yield_terminal_events
from app.domain.models import CLIEvent, EventType

logger = logging.getLogger(__name__)


class SubprocessRunner(BaseRunner):
    def __init__(self, kill_grace_sec: float = 3.0) -> None:
        super().__init__()
        self._kill_grace_sec = kill_grace_sec
        self._use_process_group = os.name == "posix" and hasattr(os, "killpg")

    async def run(
        self,
        *,
        task_id: str,
        argv: list[str],
        workdir: str,
        timeout_sec: int,
        env: dict[str, str] | None = None,
        terminal_key: str | None = None,
        interactive: bool = False,
        claude_session_id: str | None = None,
    ) -> AsyncGenerator[CLIEvent, None]:
        async for event in self.check_empty_argv(argv, task_id):
            yield event
            return

        queue: asyncio.Queue[CLIEvent | None] = asyncio.Queue()

        popen_kwargs: dict[str, Any] = {}
        if self._use_process_group:
            popen_kwargs["start_new_session"] = True

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **popen_kwargs,
            )
        except Exception as exc:
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error=f"启动失败: {exc}")
            return

        self.registry.register(task_id, process)

        logger.info(
            "subprocess task started",
            extra={
                "task_id": task_id,
                "pid": process.pid,
                "timeout_sec": timeout_sec,
                "kill_grace_sec": self._kill_grace_sec,
                "use_process_group": self._use_process_group,
            },
        )
        yield CLIEvent(type=EventType.STARTED, task_id=task_id)

        stdout_task = asyncio.create_task(
            self._pump_stream(task_id=task_id, stream=process.stdout, event_type=EventType.STDOUT, queue=queue)
        )
        stderr_task = asyncio.create_task(
            self._pump_stream(task_id=task_id, stream=process.stderr, event_type=EventType.STDERR, queue=queue)
        )
        wait_task = asyncio.create_task(asyncio.wait_for(process.wait(), timeout=timeout_sec))

        stream_done = 0
        timed_out = False
        exit_code: int | None = None
        get_task: asyncio.Task[CLIEvent | None] | None = asyncio.create_task(queue.get())

        try:
            while True:
                wait_set: set[asyncio.Task[Any]] = set()
                if get_task is not None:
                    wait_set.add(get_task)
                if not wait_task.done():
                    wait_set.add(wait_task)

                if not wait_set:
                    break

                done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

                if wait_task in done:
                    try:
                        exit_code = wait_task.result()
                    except TimeoutError:
                        timed_out = True
                        logger.warning(
                            "subprocess task timeout",
                            extra={
                                "task_id": task_id,
                                "pid": process.pid,
                                "timeout_sec": timeout_sec,
                                "kill_grace_sec": self._kill_grace_sec,
                                "use_process_group": self._use_process_group,
                            },
                        )
                        await self._terminate_then_kill(process, task_id=task_id)
                        exit_code = await process.wait()

                if get_task is not None and get_task in done:
                    item = get_task.result()
                    if item is None:
                        stream_done += 1
                        if stream_done >= 2:
                            get_task = None
                        else:
                            get_task = asyncio.create_task(queue.get())
                    else:
                        yield item
                        get_task = asyncio.create_task(queue.get())

                if wait_task.done() and stream_done >= 2:
                    break

            canceled = self.registry.is_cancelled(task_id)
            async for event in yield_terminal_events(
                task_id=task_id,
                exit_code=exit_code,
                timed_out=timed_out,
                canceled=canceled,
                timeout_sec=timeout_sec,
                log_extra=self._finish_log_extra(
                    task_id=task_id,
                    process=process,
                    timeout_sec=timeout_sec,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    canceled=canceled,
                ),
            ):
                yield event
        finally:
            for task in (stdout_task, stderr_task, wait_task):
                if not task.done():
                    task.cancel()
            if get_task is not None and not get_task.done():
                get_task.cancel()
            self.registry.unregister(task_id)

    def _finish_log_extra(
        self,
        *,
        task_id: str,
        process: asyncio.subprocess.Process,
        timeout_sec: int,
        exit_code: int | None,
        timed_out: bool,
        canceled: bool,
    ) -> dict[str, object]:
        return {
            "task_id": task_id,
            "pid": process.pid,
            "returncode": process.returncode,
            "timeout_sec": timeout_sec,
            "kill_grace_sec": self._kill_grace_sec,
            "use_process_group": self._use_process_group,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "canceled": canceled,
        }

    async def cancel(self, task_id: str) -> bool:
        async with self.registry.lock:
            entry = self.registry.get_entry(task_id)
            if entry is None:
                return False
            entry.cancel_requested = True
            process: asyncio.subprocess.Process = entry.task

        if process.returncode is not None:
            return False

        logger.info(
            "subprocess task cancel requested",
            extra={
                "task_id": task_id,
                "pid": process.pid,
                "returncode": process.returncode,
                "kill_grace_sec": self._kill_grace_sec,
                "use_process_group": self._use_process_group,
            },
        )
        await self._terminate_then_kill(process, task_id=task_id)
        return True

    async def _terminate_then_kill(self, process: asyncio.subprocess.Process, *, task_id: str | None = None) -> None:
        if process.returncode is not None:
            return

        logger.info(
            "subprocess terminate sent",
            extra={
                "task_id": task_id,
                "pid": process.pid,
                "returncode": process.returncode,
                "kill_grace_sec": self._kill_grace_sec,
                "use_process_group": self._use_process_group,
            },
        )
        self._send_signal(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._kill_grace_sec)
            return
        except TimeoutError:
            pass

        if process.returncode is None:
            logger.warning(
                "subprocess kill sent",
                extra={
                    "task_id": task_id,
                    "pid": process.pid,
                    "returncode": process.returncode,
                    "kill_grace_sec": self._kill_grace_sec,
                    "use_process_group": self._use_process_group,
                },
            )
            self._kill(process)
            await process.wait()

    def _send_signal(self, process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        if process.returncode is not None:
            return
        try:
            if self._use_process_group:
                os.killpg(process.pid, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            return

    def _kill(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if self._use_process_group:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return

    async def _pump_stream(
        self,
        *,
        task_id: str,
        stream: asyncio.StreamReader | None,
        event_type: EventType,
        queue: asyncio.Queue[CLIEvent | None],
    ) -> None:
        if stream is None:
            await queue.put(None)
            return

        try:
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                text = chunk.decode(errors="replace")
                await queue.put(CLIEvent(type=event_type, task_id=task_id, content=text))
        finally:
            await queue.put(None)
