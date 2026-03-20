from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.adapters.process.claude_stop_hook import ClaudeStopArtifacts, build_task_artifacts
from app.domain.models import CLIEvent, EventType


class SubprocessRunner:
    def __init__(self, kill_grace_sec: float = 3.0, data_dir: str = "/tmp/tg-cli-gateway") -> None:
        self._kill_grace_sec = kill_grace_sec
        self._data_dir = data_dir
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
        provider: str | None = None,
    ) -> AsyncIterator[CLIEvent]:
        if not argv:
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error="命令参数为空")
            return

        queue: asyncio.Queue[CLIEvent | None] = asyncio.Queue()
        artifacts: ClaudeStopArtifacts | None = None
        run_argv = list(argv)

        try:
            if provider == "claude_code" and not interactive:
                artifacts = build_task_artifacts(task_id=task_id, data_dir=Path(self._data_dir))
                run_argv = self._inject_claude_settings(run_argv, artifacts)

            process = await asyncio.create_subprocess_exec(
                *run_argv,
                cwd=workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            self._cleanup_artifacts(artifacts)
            yield CLIEvent(type=EventType.FAILED, task_id=task_id, error=f"启动失败: {exc}")
            return

        async with self._lock:
            self._processes[task_id] = process

        yield CLIEvent(type=EventType.STARTED, task_id=task_id)

        stdout_task = asyncio.create_task(self._pump_stream(task_id=task_id, stream=process.stdout, event_type=EventType.STDOUT, queue=queue))
        stderr_task = asyncio.create_task(self._pump_stream(task_id=task_id, stream=process.stderr, event_type=EventType.STDERR, queue=queue))
        wait_task = asyncio.create_task(asyncio.wait_for(process.wait(), timeout=timeout_sec))

        stream_done = 0
        timed_out = False
        exit_code: int | None = None
        stdout_chunks: list[str] = []
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
                        await self._terminate_then_kill(process)
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
                        if item.type == EventType.STDOUT and item.content is not None:
                            stdout_chunks.append(item.content)
                        yield item
                        get_task = asyncio.create_task(queue.get())

                if wait_task.done() and stream_done >= 2:
                    break

            reply = await self._read_response_with_retry(artifacts.response_file) if artifacts is not None else None
            stdout_text = "".join(stdout_chunks)
            should_emit_reply = reply is not None
            if should_emit_reply and provider == "claude_code" and not interactive and exit_code == 0:
                should_emit_reply = stdout_text.rstrip("\n") != reply.rstrip("\n")
            if should_emit_reply and reply is not None:
                yield CLIEvent(type=EventType.STDOUT, task_id=task_id, content=reply)

            if timed_out:
                yield CLIEvent(type=EventType.TIMEOUT, task_id=task_id, error=f"任务超时({timeout_sec}s)")
            elif task_id in self._cancel_requested:
                yield CLIEvent(type=EventType.CANCELED, task_id=task_id, error="任务已取消")
            elif exit_code == 0:
                yield CLIEvent(type=EventType.EXITED, task_id=task_id, exit_code=0)
            else:
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

            self._cleanup_artifacts(artifacts)

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            process = self._processes.get(task_id)
            if process is None:
                return False
            self._cancel_requested.add(task_id)

        if process.returncode is not None:
            return False

        process.terminate()
        return True

    async def _terminate_then_kill(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return

        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._kill_grace_sec)
            return
        except TimeoutError:
            pass

        if process.returncode is None:
            process.kill()
            await process.wait()

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

    def _inject_claude_settings(self, argv: list[str], artifacts: ClaudeStopArtifacts) -> list[str]:
        if not argv:
            return argv
        return [argv[0], "--settings", str(artifacts.settings_file), *argv[1:]]

    async def _read_response_with_retry(self, response_file: Path) -> str | None:
        for attempt in range(3):
            try:
                content = response_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                content = None
            except OSError:
                content = None
            else:
                if content.strip():
                    return content
            if attempt < 2:
                await asyncio.sleep(0.05)
        return None

    def _cleanup_artifacts(self, artifacts: ClaudeStopArtifacts | None) -> None:
        if artifacts is None:
            return
        for path in (artifacts.settings_file, artifacts.response_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
