from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

try:  # watchdog 是可选运行时依赖；不可用时自动退回 supervisor 轮询兜底。
    from watchdog.observers import Observer as _WatchdogObserver
except ImportError:  # pragma: no cover - 依赖缺失路径由 start() 的行为覆盖
    _WatchdogObserver = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
_PathLike = str | bytes | os.PathLike[str] | os.PathLike[bytes]


class _Observer(Protocol):
    def schedule(self, event_handler: Any, path: str, *, recursive: bool) -> object: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


@dataclass(frozen=True)
class _WatchEntry:
    session_id: str
    cwd: str


class _JSONLFileEventHandler:
    def __init__(self, watcher: JSONLFileWatcher) -> None:
        self._watcher = watcher

    def dispatch(self, event: Any) -> None:
        if getattr(event, "event_type", "") in {"modified", "created", "moved", "deleted"}:
            self._watcher.handle_event(event)

    def on_modified(self, event: Any) -> None:
        self._watcher.handle_event(event)

    def on_created(self, event: Any) -> None:
        self._watcher.handle_event(event)

    def on_moved(self, event: Any) -> None:
        self._watcher.handle_event(event)

    def on_deleted(self, event: Any) -> None:
        self._watcher.handle_event(event)


class JSONLFileWatcher:
    """Watch registered Claude JSONL files and notify the asyncio loop on changes."""

    def __init__(
        self,
        *,
        projects_dir: Path,
        on_change: Callable[[str, str], None],
        loop: asyncio.AbstractEventLoop | None = None,
        observer_factory: Callable[[], _Observer] | None = None,
        enabled: bool = True,
        join_timeout_sec: float = 2.0,
    ) -> None:
        self._projects_dir = projects_dir.expanduser()
        self._on_change = on_change
        self._loop = loop
        self._observer_factory = observer_factory
        self._enabled = enabled
        self._join_timeout_sec = join_timeout_sec
        self._handler = _JSONLFileEventHandler(self)
        self._observer: _Observer | None = None
        self._watched_files: dict[str, _WatchEntry] = {}
        self._session_paths: dict[str, set[str]] = {}
        self._scheduled_dirs: dict[str, object] = {}
        self._backfilled_files: set[str] = set()
        self._lock = threading.RLock()
        self._started = False
        self._available = False

    @property
    def is_available(self) -> bool:
        return self._started and self._available

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def start(self) -> bool:
        """Start the watchdog observer if possible; return whether fs events are active."""
        if not self._enabled:
            self._available = False
            return False
        if self._started:
            return self._available
        if not self._path_exists(self._projects_dir):
            logger.warning(
                "jsonl file watcher projects dir does not exist; falling back to supervisor polling",
                extra={"projects_dir": str(self._projects_dir)},
            )
            self._available = False
            return False

        factory = self._observer_factory
        if factory is None:
            if _WatchdogObserver is None:
                logger.warning("watchdog is not installed; falling back to supervisor polling")
                self._available = False
                return False
            factory = cast(Callable[[], _Observer], _WatchdogObserver)

        try:
            self._loop = self._loop or asyncio.get_running_loop()
            observer = factory()
            observer.start()
        except Exception:
            logger.warning(
                "jsonl file watcher failed to start; falling back to supervisor polling",
                extra={"projects_dir": str(self._projects_dir)},
                exc_info=True,
            )
            self._observer = None
            self._started = False
            self._available = False
            return False

        self._observer = observer
        self._started = True
        self._available = True
        self._sync_parent_dirs()
        return self._available

    def stop(self) -> None:
        """Stop the watchdog observer; safe to call repeatedly."""
        observer = self._observer
        if observer is None:
            self._started = False
            self._available = False
            return
        self._observer = None
        self._started = False
        self._available = False
        with self._lock:
            self._scheduled_dirs.clear()
        try:
            observer.stop()
            observer.join(timeout=self._join_timeout_sec)
            try:
                alive = observer.is_alive()
            except Exception:
                alive = False
            if alive:
                logger.warning("jsonl file watcher observer did not stop before timeout")
        except Exception:
            logger.warning("jsonl file watcher failed to stop cleanly", exc_info=True)

    def watch_files(self, *, session_id: str, cwd: str, paths: Iterable[Path]) -> None:
        """Register JSONL file paths for a session.

        Registration is exact-path based: unrelated files in watched parent
        directories are ignored by the path filter.
        """
        normalized_paths = {self._normalize_path(path) for path in paths}
        if not normalized_paths:
            return
        entry = _WatchEntry(session_id=session_id, cwd=cwd)
        with self._lock:
            session_paths = self._session_paths.setdefault(session_id, set())
            for path in normalized_paths:
                self._watched_files[path] = entry
                session_paths.add(path)
        self._sync_parent_dirs()

    def replace_session_files(self, *, session_id: str, cwd: str, paths: Iterable[Path]) -> None:
        normalized_paths = {self._normalize_path(path) for path in paths}
        entry = _WatchEntry(session_id=session_id, cwd=cwd)
        with self._lock:
            old_paths = self._session_paths.get(session_id, set())
            for path in old_paths - normalized_paths:
                current = self._watched_files.get(path)
                if current is not None and current.session_id == session_id:
                    self._watched_files.pop(path, None)
                    self._backfilled_files.discard(path)
            self._session_paths[session_id] = set(normalized_paths)
            for path in normalized_paths:
                self._watched_files[path] = entry
        self._sync_parent_dirs()

    def unwatch_session(self, session_id: str) -> None:
        with self._lock:
            paths = self._session_paths.pop(session_id, set())
            for path in paths:
                current = self._watched_files.get(path)
                if current is not None and current.session_id == session_id:
                    self._watched_files.pop(path, None)
                    self._backfilled_files.discard(path)
        self._sync_parent_dirs()

    def clear(self) -> None:
        observer = self._observer
        with self._lock:
            watches = list(self._scheduled_dirs.values())
            self._watched_files.clear()
            self._session_paths.clear()
            self._scheduled_dirs.clear()
            self._backfilled_files.clear()
        self._unschedule_watches(observer, watches)

    def _sync_parent_dirs(self) -> None:
        observer = self._observer
        if observer is None or not self._available:
            return
        with self._lock:
            desired_parents = {str(Path(path).parent) for path in self._watched_files}
            scheduled_dirs = set(self._scheduled_dirs)

        desired_dirs: set[str] = set()
        existing_parents: set[str] = set()
        for parent in desired_parents:
            if self._path_exists(Path(parent)):
                desired_dirs.add(parent)
                existing_parents.add(parent)
                continue
            existing_ancestor = self._nearest_existing_parent(parent)
            if existing_ancestor is not None:
                desired_dirs.add(existing_ancestor)

        for directory in sorted(desired_dirs - scheduled_dirs):
            with self._lock:
                if directory in self._scheduled_dirs:
                    continue
            try:
                watch = observer.schedule(self._handler, directory, recursive=False)
            except Exception:
                logger.warning("jsonl file watcher failed to watch directory", extra={"directory": directory}, exc_info=True)
                if self._path_exists(Path(directory)):
                    self._available = False
                    return
                continue
            with self._lock:
                stale_watch = self._scheduled_dirs.setdefault(directory, watch)
            if stale_watch is not watch:
                self._unschedule_watches(observer, (watch,))

        self._notify_existing_files(existing_parents)

        stale_watches: list[object] = []
        with self._lock:
            for directory in sorted(set(self._scheduled_dirs) - desired_dirs):
                watch = self._scheduled_dirs.pop(directory, None)
                if watch is not None:
                    stale_watches.append(watch)
        self._unschedule_watches(observer, stale_watches)

    def _notify_existing_files(self, parent_dirs: set[str]) -> None:
        if not parent_dirs:
            return
        with self._lock:
            items = []
            for path, entry in self._watched_files.items():
                if path in self._backfilled_files:
                    continue
                if str(Path(path).parent) not in parent_dirs or not self._path_exists(Path(path)):
                    continue
                self._backfilled_files.add(path)
                items.append((path, entry))
        for path, entry in items:
            self._notify(entry, path)

    def handle_event(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            self._handle_directory_event(event)
            return
        candidate_paths = [getattr(event, "dest_path", None), getattr(event, "src_path", None)]
        for raw_path in candidate_paths:
            if raw_path is None:
                continue
            path = self._normalize_raw_path(raw_path)
            entry = self._entry_for_path(path)
            if entry is None:
                continue
            self._notify(entry, path)
            return

    def _handle_directory_event(self, event: Any) -> None:
        event_type = getattr(event, "event_type", "")
        if event_type in {"deleted", "moved"}:
            changed_dirs = {
                self._normalize_raw_path(raw_path)
                for raw_path in (getattr(event, "dest_path", None), getattr(event, "src_path", None))
                if raw_path is not None
            }
            self._drop_scheduled_dirs(changed_dirs)
        self._sync_parent_dirs()

    def _drop_scheduled_dirs(self, directories: set[str]) -> None:
        if not directories:
            return
        observer = self._observer
        with self._lock:
            watches = [self._scheduled_dirs.pop(directory) for directory in directories if directory in self._scheduled_dirs]
        self._unschedule_watches(observer, watches)

    def _entry_for_path(self, path: str) -> _WatchEntry | None:
        with self._lock:
            return self._watched_files.get(path)

    def _notify(self, entry: _WatchEntry, path: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.debug("jsonl file watcher skipped event because loop is unavailable", extra={"path": path})
            return
        try:
            loop.call_soon_threadsafe(self._emit_change, entry.session_id, entry.cwd, path)
        except RuntimeError:
            logger.debug("jsonl file watcher skipped event because loop is closed", extra={"path": path}, exc_info=True)

    def _emit_change(self, session_id: str, cwd: str, path: str) -> None:
        try:
            self._on_change(session_id, cwd)
        except Exception:
            logger.exception("jsonl file watcher callback failed", extra={"session_id": session_id, "path": path})

    def _nearest_existing_parent(self, parent: str) -> str | None:
        projects_dir = self._normalize_path(self._projects_dir)
        current = Path(parent)
        while True:
            if self._path_exists(current):
                return str(current)
            current_str = str(current)
            if current_str == projects_dir or current == current.parent:
                return projects_dir if self._path_exists(Path(projects_dir)) else None
            current = current.parent

    def _path_exists(self, path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            logger.debug("jsonl file watcher could not stat path", extra={"path": str(path)}, exc_info=True)
            return False

    def _unschedule_watches(self, observer: _Observer | None, watches: Iterable[object]) -> None:
        if observer is None:
            return
        unschedule = getattr(observer, "unschedule", None)
        if not callable(unschedule):
            return
        for watch in watches:
            try:
                unschedule(watch)
            except Exception:
                logger.warning("jsonl file watcher failed to remove directory watch", exc_info=True)

    def _normalize_path(self, path: Path) -> str:
        return str(path.expanduser().resolve(strict=False))

    def _normalize_raw_path(self, path: _PathLike) -> str:
        return str(Path(os.fsdecode(path)).expanduser().resolve(strict=False))
