"""Unit tests for JSONLFileWatcher and _DebouncedHandler."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from app.services.jsonl_file_watcher import JSONLFileWatcher, _DebouncedHandler


class TestDebouncedHandler:
    def test_handle_path_registers_watched_file(self, tmp_path: Path) -> None:
        """Handler only fires for files registered in watched_files."""
        callback = MagicMock()
        watched: dict[str, tuple[str, str]] = {}
        handler = _DebouncedHandler(watched_files=watched, debounce_sec=0.05, on_change=callback)

        # Register a file
        file_path = str(tmp_path / "test.jsonl")
        watched[file_path] = ("session-1", "/workdir")

        # Simulate event
        event = MagicMock()
        event.is_directory = False
        event.src_path = file_path
        handler.on_modified(event)

        # Wait for debounce
        time.sleep(0.15)
        callback.assert_called_once_with("session-1", "/workdir")

    def test_handle_path_ignores_unwatched_file(self, tmp_path: Path) -> None:
        """Handler ignores files not in watched_files."""
        callback = MagicMock()
        watched: dict[str, tuple[str, str]] = {}
        handler = _DebouncedHandler(watched_files=watched, debounce_sec=0.05, on_change=callback)

        event = MagicMock()
        event.is_directory = False
        event.src_path = str(tmp_path / "unwatched.jsonl")
        handler.on_modified(event)

        time.sleep(0.15)
        callback.assert_not_called()

    def test_debounce_coalesces_rapid_events(self, tmp_path: Path) -> None:
        """Multiple rapid events for the same session should fire only once."""
        callback = MagicMock()
        watched: dict[str, tuple[str, str]] = {}
        handler = _DebouncedHandler(watched_files=watched, debounce_sec=0.1, on_change=callback)

        file_path = str(tmp_path / "test.jsonl")
        watched[file_path] = ("session-1", "/workdir")

        event = MagicMock()
        event.is_directory = False
        event.src_path = file_path

        # Fire 3 events rapidly
        handler.on_modified(event)
        handler.on_modified(event)
        handler.on_modified(event)

        time.sleep(0.3)
        # Should fire only once due to debounce
        assert callback.call_count == 1

    def test_on_created_triggers_callback(self, tmp_path: Path) -> None:
        """on_created events should trigger the callback."""
        callback = MagicMock()
        watched: dict[str, tuple[str, str]] = {}
        handler = _DebouncedHandler(watched_files=watched, debounce_sec=0.05, on_change=callback)

        file_path = str(tmp_path / "new.jsonl")
        watched[file_path] = ("session-1", "/workdir")

        event = MagicMock()
        event.is_directory = False
        event.src_path = file_path
        handler.on_created(event)

        time.sleep(0.15)
        callback.assert_called_once_with("session-1", "/workdir")

    def test_on_moved_triggers_callback_for_dest(self, tmp_path: Path) -> None:
        """on_moved events should trigger callback for dest_path."""
        callback = MagicMock()
        watched: dict[str, tuple[str, str]] = {}
        handler = _DebouncedHandler(watched_files=watched, debounce_sec=0.05, on_change=callback)

        file_path = str(tmp_path / "moved.jsonl")
        watched[file_path] = ("session-1", "/workdir")

        event = MagicMock()
        event.is_directory = False
        event.dest_path = file_path
        handler.on_moved(event)

        time.sleep(0.15)
        callback.assert_called_once_with("session-1", "/workdir")

    def test_callback_exception_does_not_break_handler(self, tmp_path: Path) -> None:
        """Exception in callback should not prevent subsequent events from firing."""
        call_count = 0

        def failing_callback(session_id: str, cwd: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")

        watched: dict[str, tuple[str, str]] = {}
        handler = _DebouncedHandler(watched_files=watched, debounce_sec=0.05, on_change=failing_callback)

        file_path = str(tmp_path / "test.jsonl")
        watched[file_path] = ("session-1", "/workdir")

        event = MagicMock()
        event.is_directory = False
        event.src_path = file_path

        # First event raises
        handler.on_modified(event)
        time.sleep(0.15)

        # Second event should still work
        handler.on_modified(event)
        time.sleep(0.15)

        assert call_count == 2

    def test_handle_path_with_bytes_src_path(self, tmp_path: Path) -> None:
        """Handler should handle bytes src_path (watchdog on some platforms)."""
        callback = MagicMock()
        watched: dict[str, tuple[str, str]] = {}
        handler = _DebouncedHandler(watched_files=watched, debounce_sec=0.05, on_change=callback)

        file_path = str(tmp_path / "test.jsonl")
        watched[file_path] = ("session-1", "/workdir")

        event = MagicMock()
        event.is_directory = False
        event.src_path = file_path.encode()
        handler.on_modified(event)

        time.sleep(0.15)
        callback.assert_called_once_with("session-1", "/workdir")

    def test_cancel_all_stops_pending_timers(self, tmp_path: Path) -> None:
        """cancel_all should prevent pending callbacks from firing."""
        callback = MagicMock()
        watched: dict[str, tuple[str, str]] = {}
        handler = _DebouncedHandler(watched_files=watched, debounce_sec=0.2, on_change=callback)

        file_path = str(tmp_path / "test.jsonl")
        watched[file_path] = ("session-1", "/workdir")

        event = MagicMock()
        event.is_directory = False
        event.src_path = file_path
        handler.on_modified(event)

        # Cancel before debounce fires
        handler.cancel_all()
        time.sleep(0.3)

        callback.assert_not_called()


class TestJSONLFileWatcher:
    def test_add_registers_file_in_watched_dict(self, tmp_path: Path) -> None:
        watcher = JSONLFileWatcher(
            projects_dir=tmp_path,
            debounce_sec=0.1,
            on_change=lambda sid, cwd: None,
        )
        watcher.add("session-1", "/workdir")

        assert len(watcher._watched_files) == 1
        key = list(watcher._watched_files.keys())[0]
        assert watcher._watched_files[key] == ("session-1", "/workdir")

    def test_remove_unregisters_session(self, tmp_path: Path) -> None:
        watcher = JSONLFileWatcher(
            projects_dir=tmp_path,
            debounce_sec=0.1,
            on_change=lambda sid, cwd: None,
        )
        watcher.add("session-1", "/workdir")
        assert len(watcher._watched_files) == 1

        watcher.remove("session-1")
        assert len(watcher._watched_files) == 0

    def test_remove_nonexistent_session_is_noop(self, tmp_path: Path) -> None:
        watcher = JSONLFileWatcher(
            projects_dir=tmp_path,
            debounce_sec=0.1,
            on_change=lambda sid, cwd: None,
        )
        watcher.remove("nonexistent")  # Should not raise

    def test_start_creates_observer_for_existing_dir(self, tmp_path: Path) -> None:
        watcher = JSONLFileWatcher(
            projects_dir=tmp_path,
            debounce_sec=0.1,
            on_change=lambda sid, cwd: None,
        )
        watcher.start()
        assert watcher._started is True

        watcher.stop()
        assert watcher._started is False

    def test_start_noop_for_missing_dir(self, tmp_path: Path) -> None:
        watcher = JSONLFileWatcher(
            projects_dir=tmp_path / "nonexistent",
            debounce_sec=0.1,
            on_change=lambda sid, cwd: None,
        )
        watcher.start()
        assert watcher._started is False

    def test_start_idempotent(self, tmp_path: Path) -> None:
        watcher = JSONLFileWatcher(
            projects_dir=tmp_path,
            debounce_sec=0.1,
            on_change=lambda sid, cwd: None,
        )
        watcher.start()
        watcher.start()  # Should not raise or create second observer

        watcher.stop()

    def test_stop_idempotent(self, tmp_path: Path) -> None:
        watcher = JSONLFileWatcher(
            projects_dir=tmp_path,
            debounce_sec=0.1,
            on_change=lambda sid, cwd: None,
        )
        watcher.stop()  # Should not raise when not started

    def test_add_sanitizes_session_id(self, tmp_path: Path) -> None:
        """Session IDs with path separators should be sanitized."""
        watcher = JSONLFileWatcher(
            projects_dir=tmp_path,
            debounce_sec=0.1,
            on_change=lambda sid, cwd: None,
        )
        watcher.add("session/with/slashes", "/workdir")

        key = list(watcher._watched_files.keys())[0]
        assert "/" not in key.split("/")[-1]  # No slashes in filename part
