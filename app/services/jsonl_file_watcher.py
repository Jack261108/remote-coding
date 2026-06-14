"""Watchdog-based watcher for Claude JSONL session files.

Monitors ``~/.claude/projects/`` for modifications to ``*.jsonl`` files and
triggers callbacks when changes are detected, replacing the polling-based
approach in SessionSupervisor.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class _DebouncedHandler(FileSystemEventHandler):
    """Debounce rapid file-system events before notifying."""

    def __init__(
        self,
        *,
        watched_files: dict[str, tuple[str, str]],
        debounce_sec: float,
        on_change: Callable[[str, str], None],
    ) -> None:
        super().__init__()
        self._watched_files = watched_files
        self._debounce_sec = debounce_sec
        self._on_change = on_change
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_modified(self, event: FileSystemEvent) -> None:  # noqa: ANN001
        if not event.is_directory:
            self._handle_path(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:  # noqa: ANN001
        if not event.is_directory:
            self._handle_path(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:  # noqa: ANN001
        if not event.is_directory:
            self._handle_path(event.dest_path)

    def _handle_path(self, path: bytes | str) -> None:
        if isinstance(path, bytes):
            path = path.decode(errors="surrogateescape")
        entry = self._watched_files.get(path)
        if entry is None:
            return
        session_id, cwd = entry
        with self._lock:
            existing = self._timers.get(session_id)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(
                self._debounce_sec,
                self._fire,
                args=[session_id, cwd],
            )
            timer.daemon = True
            self._timers[session_id] = timer
            timer.start()

    def _fire(self, session_id: str, cwd: str) -> None:
        with self._lock:
            self._timers.pop(session_id, None)
        try:
            self._on_change(session_id, cwd)
        except Exception:
            logger.exception("jsonl watcher callback failed", extra={"session_id": session_id})

    def cancel_all(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()


class JSONLFileWatcher:
    """Monitor Claude JSONL session files using watchdog.

    Usage::

        watcher = JSONLFileWatcher(
            projects_dir=Path("~/.claude/projects").expanduser(),
            debounce_sec=0.1,
            on_change=lambda sid: print(f"changed: {sid}"),
        )
        watcher.start()
        watcher.add("session-id-1", "/some/workdir")
        ...
        watcher.stop()
    """

    def __init__(
        self,
        *,
        projects_dir: Path,
        debounce_sec: float,
        on_change: Callable[[str, str], None],
    ) -> None:
        self._projects_dir = projects_dir
        self._debounce_sec = debounce_sec
        self._on_change = on_change
        # file_path -> (session_id, cwd)
        # Thread safety: watchdog thread reads via .get() (GIL-protected);
        # add()/remove() are called from the asyncio thread only.
        self._watched_files: dict[str, tuple[str, str]] = {}
        self._handler = _DebouncedHandler(
            watched_files=self._watched_files,
            debounce_sec=debounce_sec,
            on_change=on_change,
        )
        self._observer = Observer()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        if self._projects_dir.exists():
            self._observer.schedule(self._handler, str(self._projects_dir), recursive=True)
            self._observer.start()
            self._started = True
            logger.debug("jsonl file watcher started", extra={"projects_dir": str(self._projects_dir)})

    def add(self, session_id: str, cwd: str) -> None:
        """Register a session JSONL file for monitoring."""
        safe_session_id = session_id.replace("/", "-").replace(".", "-")
        project_dir = cwd.replace("/", "-").replace(".", "-")
        jsonl_path = str(self._projects_dir / project_dir / f"{safe_session_id}.jsonl")
        self._watched_files[jsonl_path] = (session_id, cwd)

    def remove(self, session_id: str) -> None:
        """Unregister a session from monitoring."""
        to_remove = [k for k, v in self._watched_files.items() if v[0] == session_id]
        for k in to_remove:
            self._watched_files.pop(k, None)

    def stop(self) -> None:
        """Stop the watchdog observer."""
        if not self._started:
            return
        self._handler.cancel_all()
        self._observer.stop()
        self._observer.join(timeout=2.0)
        if self._observer.is_alive():
            logger.warning("jsonl file watcher did not stop within timeout, callbacks may still fire")
        self._started = False
        logger.debug("jsonl file watcher stopped")
