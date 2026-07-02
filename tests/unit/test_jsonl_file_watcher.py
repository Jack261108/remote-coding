from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.jsonl_file_watcher import JSONLFileWatcher


class _FakeObserver:
    def __init__(self, *, fail_start: bool = False, alive_after_join: bool = False) -> None:
        self.fail_start = fail_start
        self.alive_after_join = alive_after_join
        self.handler = None
        self.scheduled_path: str | None = None
        self.recursive: bool | None = None
        self.started = 0
        self.stopped = 0
        self.join_timeout: float | None = None

    def schedule(self, event_handler, path: str, *, recursive: bool) -> object:
        self.handler = event_handler
        self.scheduled_path = path
        self.recursive = recursive
        return object()

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("observer failed")
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def join(self, timeout: float | None = None) -> None:
        self.join_timeout = timeout

    def is_alive(self) -> bool:
        return self.alive_after_join


def _event(path: str | bytes, *, event_type: str = "modified", dest_path: str | bytes | None = None, is_directory: bool = False):
    return SimpleNamespace(src_path=path, dest_path=dest_path, is_directory=is_directory, event_type=event_type)


def _ignore_change(session_id: str, cwd: str) -> None:
    _ = (session_id, cwd)


@pytest.mark.asyncio
async def test_jsonl_file_watcher_triggers_only_registered_files(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    observer = _FakeObserver()
    calls: list[tuple[str, str]] = []
    watcher = JSONLFileWatcher(
        projects_dir=projects_dir,
        on_change=lambda session_id, cwd: calls.append((session_id, cwd)),
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: observer,
    )
    watched_file = projects_dir / "-tmp-work" / "claude-session-1.jsonl"
    other_file = projects_dir / "-tmp-work" / "other.jsonl"

    assert watcher.start() is True
    watcher.watch_files(session_id="claude-session-1", cwd="/tmp/work", paths=(watched_file,))

    watcher.handle_event(_event(str(other_file)))
    watcher.handle_event(_event(str(watched_file), is_directory=True))
    watcher.handle_event(_event(str(watched_file)))
    await asyncio.sleep(0)

    assert calls == [("claude-session-1", "/tmp/work")]
    assert observer.scheduled_path == str(projects_dir)
    assert observer.recursive is True


@pytest.mark.asyncio
async def test_jsonl_file_watcher_handles_created_moved_and_bytes_paths(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    calls: list[tuple[str, str]] = []
    watcher = JSONLFileWatcher(
        projects_dir=projects_dir,
        on_change=lambda session_id, cwd: calls.append((session_id, cwd)),
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: _FakeObserver(),
    )
    watched_file = projects_dir / "-tmp-work" / "claude-session-1.jsonl"
    moved_from = projects_dir / "-tmp-work" / "tmp.jsonl"
    watcher.watch_files(session_id="claude-session-1", cwd="/tmp/work", paths=(watched_file,))

    watcher.handle_event(_event(str(watched_file), event_type="created"))
    watcher.handle_event(_event(str(moved_from), event_type="moved", dest_path=str(watched_file)))
    watcher.handle_event(_event(bytes(watched_file), event_type="modified"))
    await asyncio.sleep(0)

    assert calls == [
        ("claude-session-1", "/tmp/work"),
        ("claude-session-1", "/tmp/work"),
        ("claude-session-1", "/tmp/work"),
    ]


@pytest.mark.asyncio
async def test_jsonl_file_watcher_replaces_and_clears_session_files(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    calls: list[tuple[str, str]] = []
    watcher = JSONLFileWatcher(
        projects_dir=projects_dir,
        on_change=lambda session_id, cwd: calls.append((session_id, cwd)),
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: _FakeObserver(),
    )
    old_file = projects_dir / "old.jsonl"
    new_file = projects_dir / "new.jsonl"

    watcher.watch_files(session_id="sid", cwd="/tmp/old", paths=(old_file,))
    watcher.replace_session_files(session_id="sid", cwd="/tmp/new", paths=(new_file,))
    watcher.handle_event(_event(str(old_file)))
    watcher.handle_event(_event(str(new_file)))
    await asyncio.sleep(0)
    assert calls == [("sid", "/tmp/new")]

    watcher.clear()
    watcher.handle_event(_event(str(new_file)))
    await asyncio.sleep(0)
    assert calls == [("sid", "/tmp/new")]


@pytest.mark.asyncio
async def test_jsonl_file_watcher_start_stop_is_idempotent(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    observer = _FakeObserver()
    watcher = JSONLFileWatcher(
        projects_dir=projects_dir,
        on_change=_ignore_change,
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: observer,
        join_timeout_sec=0.25,
    )

    assert watcher.start() is True
    assert watcher.start() is True
    watcher.stop()
    watcher.stop()

    assert observer.started == 1
    assert observer.stopped == 1
    assert observer.join_timeout == 0.25
    assert watcher.is_available is False


@pytest.mark.asyncio
async def test_jsonl_file_watcher_start_failure_falls_back(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    watcher = JSONLFileWatcher(
        projects_dir=projects_dir,
        on_change=_ignore_change,
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: _FakeObserver(fail_start=True),
    )
    caplog.set_level("WARNING", logger="app.services.jsonl_file_watcher")

    assert watcher.start() is False
    assert watcher.is_available is False
    assert any(record.message == "jsonl file watcher failed to start; falling back to supervisor polling" for record in caplog.records)


@pytest.mark.asyncio
async def test_jsonl_file_watcher_missing_projects_dir_falls_back(tmp_path: Path) -> None:
    watcher = JSONLFileWatcher(
        projects_dir=tmp_path / "missing",
        on_change=_ignore_change,
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: _FakeObserver(),
    )

    assert watcher.start() is False
    assert watcher.is_available is False
