from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.jsonl_file_watcher import JSONLFileWatcher


class _FakeObserver:
    def __init__(
        self,
        *,
        fail_start: bool = False,
        alive_after_join: bool = False,
        fail_schedule_paths: set[str] | None = None,
    ) -> None:
        self.fail_start = fail_start
        self.alive_after_join = alive_after_join
        self.fail_schedule_paths = fail_schedule_paths or set()
        self.handler = None
        self.scheduled_path: str | None = None
        self.recursive: bool | None = None
        self.scheduled: list[tuple[str, bool]] = []
        self.unscheduled: list[object] = []
        self.started = 0
        self.stopped = 0
        self.join_timeout: float | None = None

    def schedule(self, event_handler, path: str, *, recursive: bool) -> object:
        if path in self.fail_schedule_paths:
            raise RuntimeError("schedule failed")
        self.handler = event_handler
        self.scheduled_path = path
        self.recursive = recursive
        self.scheduled.append((path, recursive))
        return f"watch:{path}"

    def unschedule(self, watch: object) -> None:
        self.unscheduled.append(watch)

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
    watched_file.parent.mkdir(parents=True)

    assert watcher.start() is True
    watcher.watch_files(session_id="claude-session-1", cwd="/tmp/work", paths=(watched_file,))

    watcher.handle_event(_event(str(other_file)))
    watcher.handle_event(_event(str(watched_file), is_directory=True))
    watcher.handle_event(_event(str(watched_file)))
    await asyncio.sleep(0)

    assert calls == [("claude-session-1", "/tmp/work")]
    assert observer.scheduled_path == str(watched_file.parent)
    assert observer.recursive is False
    assert observer.scheduled == [(str(watched_file.parent), False)]


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
async def test_jsonl_file_watcher_schedules_each_parent_directory_once(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    observer = _FakeObserver()
    watcher = JSONLFileWatcher(
        projects_dir=projects_dir,
        on_change=_ignore_change,
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: observer,
    )
    first_file = projects_dir / "-tmp-work" / "claude-session-1.jsonl"
    second_file = projects_dir / "-tmp-work" / "agent-agent-1.jsonl"
    other_dir_file = projects_dir / "-tmp-other" / "claude-session-2.jsonl"
    first_file.parent.mkdir(parents=True)
    other_dir_file.parent.mkdir(parents=True)

    assert watcher.start() is True
    watcher.watch_files(session_id="sid-1", cwd="/tmp/work", paths=(first_file, second_file))
    watcher.replace_session_files(session_id="sid-1", cwd="/tmp/work", paths=(first_file, second_file, other_dir_file))

    assert observer.scheduled == [(str(first_file.parent), False), (str(other_dir_file.parent), False)]


@pytest.mark.asyncio
async def test_jsonl_file_watcher_watches_existing_ancestor_until_parent_exists(tmp_path: Path) -> None:
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

    assert watcher.start() is True
    watcher.watch_files(session_id="sid-1", cwd="/tmp/work", paths=(watched_file,))

    assert watcher.is_available is True
    assert observer.scheduled == [(str(projects_dir), False)]

    watched_file.parent.mkdir(parents=True)
    watched_file.write_text("{}\n", encoding="utf-8")
    assert observer.handler is not None
    observer.handler.dispatch(_event(str(watched_file.parent), event_type="created", is_directory=True))
    await asyncio.sleep(0)

    assert observer.scheduled == [(str(projects_dir), False), (str(watched_file.parent), False)]
    assert observer.unscheduled == [f"watch:{projects_dir}"]
    assert calls == [("sid-1", "/tmp/work")]


@pytest.mark.asyncio
async def test_jsonl_file_watcher_start_backfills_registered_files(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    observer = _FakeObserver()
    watcher = JSONLFileWatcher(
        projects_dir=projects_dir,
        on_change=_ignore_change,
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: observer,
    )
    watched_file = projects_dir / "-tmp-work" / "claude-session-1.jsonl"
    watched_file.parent.mkdir(parents=True)

    watcher.watch_files(session_id="sid-1", cwd="/tmp/work", paths=(watched_file,))
    assert observer.scheduled == []

    assert watcher.start() is True

    assert observer.scheduled == [(str(watched_file.parent), False)]


@pytest.mark.asyncio
async def test_jsonl_file_watcher_releases_unused_parent_watches(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    observer = _FakeObserver()
    watcher = JSONLFileWatcher(
        projects_dir=projects_dir,
        on_change=_ignore_change,
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: observer,
    )
    old_file = projects_dir / "-tmp-old" / "claude-session-1.jsonl"
    new_file = projects_dir / "-tmp-new" / "claude-session-1.jsonl"
    old_file.parent.mkdir(parents=True)
    new_file.parent.mkdir(parents=True)

    assert watcher.start() is True
    watcher.watch_files(session_id="sid-1", cwd="/tmp/old", paths=(old_file,))
    watcher.replace_session_files(session_id="sid-1", cwd="/tmp/new", paths=(new_file,))

    assert observer.unscheduled == [f"watch:{old_file.parent}"]

    watcher.unwatch_session("sid-1")

    assert observer.unscheduled == [f"watch:{old_file.parent}", f"watch:{new_file.parent}"]


@pytest.mark.asyncio
async def test_jsonl_file_watcher_clear_unschedules_active_watches(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    observer = _FakeObserver()
    watcher = JSONLFileWatcher(
        projects_dir=projects_dir,
        on_change=_ignore_change,
        loop=asyncio.get_running_loop(),
        observer_factory=lambda: observer,
    )
    watched_file = projects_dir / "-tmp-work" / "claude-session-1.jsonl"
    watched_file.parent.mkdir(parents=True)

    assert watcher.start() is True
    watcher.watch_files(session_id="sid-1", cwd="/tmp/work", paths=(watched_file,))
    watcher.clear()

    assert observer.unscheduled == [f"watch:{watched_file.parent}"]

    watcher.watch_files(session_id="sid-1", cwd="/tmp/work", paths=(watched_file,))

    assert observer.scheduled == [(str(watched_file.parent), False), (str(watched_file.parent), False)]


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
