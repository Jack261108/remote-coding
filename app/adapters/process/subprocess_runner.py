from __future__ import annotations

import asyncio
import codecs
import logging
import os
import signal
from collections.abc import AsyncGenerator
from typing import Any

from app.adapters.process.base_runner import BaseRunner, yield_terminal_events
from app.domain.models import CLIEvent, EventType

logger = logging.getLogger(__name__)

# 按固定大小读取，避免 readline 对 AI CLI 超长单行 JSON 施加长度上限。
_STREAM_CHUNK_SIZE = 64 * 1024


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
        stdout_task = asyncio.create_task(
            self._pump_stream(task_id=task_id, stream=process.stdout, event_type=EventType.STDOUT, queue=queue)
        )
        stderr_task = asyncio.create_task(
            self._pump_stream(task_id=task_id, stream=process.stderr, event_type=EventType.STDERR, queue=queue)
        )
        wait_task = asyncio.create_task(self._wait_with_timeout(process, timeout_sec))

        stream_done = 0
        timed_out = False
        exit_code: int | None = None
        get_task: asyncio.Task[CLIEvent | None] | None = asyncio.create_task(queue.get())

        # yield 必须在 try 内：消费方放弃事件流时 GeneratorExit 会在挂起的
        # yield 点抛出，只有位于 try 内 finally 才能回收进程与后台任务。
        try:
            yield CLIEvent(type=EventType.STARTED, task_id=task_id)

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
            pending: list[asyncio.Task[Any]] = [stdout_task, stderr_task, wait_task]
            if get_task is not None:
                pending.append(get_task)
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # 事件流被消费方放弃（或循环异常退出）时进程可能仍在运行，必须回收。
            if self._termination_target_exists(process):
                logger.warning(
                    "subprocess stream abandoned before exit, terminating process",
                    extra={"task_id": task_id, "pid": process.pid},
                )
                await self._terminate_then_kill(process, task_id=task_id)
            self.registry.unregister(task_id)

    @staticmethod
    async def _wait_with_timeout(process: asyncio.subprocess.Process, timeout_sec: int) -> int:
        # 延迟创建 process.wait() 协程：若外层 task 在首次调度前被取消，
        # 直接传给 wait_for 的协程会触发 "coroutine was never awaited"。
        return await asyncio.wait_for(process.wait(), timeout=timeout_sec)

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

        if not self._termination_target_exists(process):
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
        if not self._termination_target_exists(process):
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
        started_at = asyncio.get_running_loop().time()
        self._send_signal(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._kill_grace_sec)
        except TimeoutError:
            pass

        if self._use_process_group:
            remaining = self._kill_grace_sec - (asyncio.get_running_loop().time() - started_at)
            if remaining > 0 and self._process_group_exists(process.pid):
                await asyncio.sleep(remaining)
            if not self._process_group_exists(process.pid):
                return
        elif process.returncode is not None:
            return

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
        if process.returncode is None:
            await process.wait()

    def _termination_target_exists(self, process: asyncio.subprocess.Process) -> bool:
        if self._use_process_group:
            return self._process_group_exists(process.pid)
        return process.returncode is None

    @staticmethod
    def _process_group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _send_signal(self, process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        if not self._use_process_group and process.returncode is not None:
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
        if not self._use_process_group and process.returncode is not None:
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

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        line_parts: list[str] = []

        try:
            while True:
                chunk = await stream.read(_STREAM_CHUNK_SIZE)
                eof = not chunk
                text = decoder.decode(chunk, final=eof)
                start = 0

                while True:
                    newline = text.find("\n", start)
                    if newline < 0:
                        if start < len(text):
                            line_parts.append(text[start:])
                        break
                    line_parts.append(text[start : newline + 1])
                    await queue.put(CLIEvent(type=event_type, task_id=task_id, content="".join(line_parts)))
                    line_parts.clear()
                    start = newline + 1

                if eof:
                    if line_parts:
                        await queue.put(CLIEvent(type=event_type, task_id=task_id, content="".join(line_parts)))
                    break
        finally:
            await queue.put(None)
