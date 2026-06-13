from __future__ import annotations

import asyncio
import logging
import os
import shlex
from pathlib import Path

from app.domain.models import CLIEvent, EventType

logger = logging.getLogger(__name__)


class AsyncFifoReader:
    """Event-driven reader for tmux pipe-pane output via named pipe (FIFO).

    Instead of polling a log file, this class creates a FIFO and spawns
    ``cat`` as a subprocess to read from it.  tmux ``pipe-pane`` writes
    to the same FIFO, so data flows through the pipe in real time.
    """

    def __init__(self, fifo_path: Path) -> None:
        self._fifo_path = fifo_path
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        """Create FIFO and start reader subprocess."""
        self._fifo_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self._fifo_path)
        self._process = await asyncio.create_subprocess_exec(
            "cat",
            str(self._fifo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    def pipe_command(self) -> str:
        """Return the tmux pipe-pane command string."""
        return f"cat > {shlex.quote(str(self._fifo_path))}"

    async def readlines(self) -> asyncio.StreamReader:
        """Return the stdout stream for async line iteration."""
        assert self._process is not None and self._process.stdout is not None
        return self._process.stdout

    async def close(self) -> None:
        """Clean up: terminate subprocess and remove FIFO."""
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=1.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
            self._process = None
        try:
            self._fifo_path.unlink(missing_ok=True)
        except OSError:
            pass


class TmuxLogMixin:
    def _read_new_text(self, path: Path, position: int) -> tuple[str, int]:
        if not path.exists():
            return "", position
        file_size = path.stat().st_size
        if position > file_size:
            position = 0
        with path.open("rb") as fh:
            fh.seek(position)
            data = fh.read()
            return data.decode("utf-8", errors="replace"), fh.tell()

    def _split_to_events(self, *, task_id: str, text: str) -> tuple[str, list[CLIEvent]]:
        events: list[CLIEvent] = []
        parts = text.splitlines(keepends=True)
        partial = ""
        for chunk in parts:
            if chunk.endswith("\n"):
                events.append(CLIEvent(type=EventType.STDOUT, task_id=task_id, content=chunk))
            else:
                partial += chunk
        return partial, events

    def _read_exit_code(self, path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except Exception:
            return None

    def _safe_unlink(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            return

    def _interactive_log_position(self, path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0
        except Exception:
            return 0
