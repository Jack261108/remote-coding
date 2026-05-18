from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator
from typing import Any

from app.domain.models import CLIEvent, EventType

logger = logging.getLogger(__name__)


class SubprocessRunner:
    def __init__(self, kill_grace_sec: float = 3.0) -> None:
        self._kill_grace_sec = kill_grace_sec
        self._use_process_group = os.name == "posix" and hasattr(os, "killpg")
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancel_requested: set[str] = set()
        self._lock = asyncio.Lock()

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
    ) -> AsyncIterator[CLIEvent]:
        if not argv:
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error="命令参数为空")
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

        async with self._lock:
            self._processes[task_id] = process

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

            canceled = task_id in self._cancel_requested
            if timed_out:
                logger.warning(
                    "subprocess task finished",
                    extra=self._finish_log_extra(
                        task_id=task_id,
                        process=process,
                        timeout_sec=timeout_sec,
                        result="timeout",
                        exit_code=exit_code,
                        timed_out=True,
                        canceled=canceled,
                    ),
                )
                yield CLIEvent(type=EventType.TIMEOUT, task_id=task_id, error=f"任务超时({timeout_sec}s)")
            elif canceled:
                logger.info(
                    "subprocess task finished",
                    extra=self._finish_log_extra(
                        task_id=task_id,
                        process=process,
                        timeout_sec=timeout_sec,
                        result="canceled",
                        exit_code=exit_code,
                        timed_out=False,
                        canceled=True,
                    ),
                )
                yield CLIEvent(type=EventType.CANCELED, task_id=task_id, error="任务已取消")
            elif exit_code == 0:
                logger.info(
                    "subprocess task finished",
                    extra=self._finish_log_extra(
                        task_id=task_id,
                        process=process,
                        timeout_sec=timeout_sec,
                        result="exited",
                        exit_code=exit_code,
                        timed_out=False,
                        canceled=False,
                    ),
                )
                yield CLIEvent(type=EventType.EXITED, task_id=task_id, exit_code=0)
            else:
                logger.error(
                    "subprocess task finished",
                    extra=self._finish_log_extra(
                        task_id=task_id,
                        process=process,
                        timeout_sec=timeout_sec,
                        result="failed",
                        exit_code=exit_code,
                        timed_out=False,
                        canceled=False,
                    ),
                )
                yield CLIEvent(
                    type=EventType.FAILED,
                    task_id=task_id,
                    exit_code=exit_code,
                    error=f"进程退出码: {exit_code}",
                )
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            if get_task is not None and not get_task.done():
                get_task.cancel()

            async with self._lock:
                self._processes.pop(task_id, None)
                self._cancel_requested.discard(task_id)

    def _finish_log_extra(
        self,
        *,
        task_id: str,
        process: asyncio.subprocess.Process,
        timeout_sec: int,
        result: str,
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
            "result": result,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "canceled": canceled,
        }

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            process = self._processes.get(task_id)
            if process is None:
                return False
            self._cancel_requested.add(task_id)

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
