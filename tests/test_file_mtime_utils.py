"""Tests for file_mtime_utils.

Covers: refresh_seen_mtimes, clear_seen_mtimes_for_session.
"""

from __future__ import annotations

import time

import pytest

from app.infra.file_mtime_utils import clear_seen_mtimes_for_session, refresh_seen_mtimes


class TestRefreshSeenMtimes:
    def test_detects_new_files(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        seen: dict[str, float] = {}

        updated = refresh_seen_mtimes({str(f)}, seen)

        assert str(f) in updated
        assert seen[str(f)] == updated[str(f)]

    def test_detects_modified_files(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        seen: dict[str, float] = {}

        refresh_seen_mtimes({str(f)}, seen)
        old_mtime = seen[str(f)]

        # Modify file
        time.sleep(0.05)
        f.write_text("world")

        updated = refresh_seen_mtimes({str(f)}, seen)
        assert str(f) in updated
        assert seen[str(f)] > old_mtime

    def test_skips_unchanged_files(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        seen: dict[str, float] = {}

        refresh_seen_mtimes({str(f)}, seen)
        updated = refresh_seen_mtimes({str(f)}, seen)

        assert str(f) not in updated

    def test_skips_missing_files(self, tmp_path):
        seen: dict[str, float] = {}
        updated = refresh_seen_mtimes({str(tmp_path / "missing.txt")}, seen)
        assert updated == {}

    def test_handles_multiple_paths(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a")
        f2.write_text("b")
        seen: dict[str, float] = {}

        updated = refresh_seen_mtimes({str(f1), str(f2)}, seen)
        assert len(updated) == 2


class TestClearSeenMtimesForSession:
    def test_removes_matching_keys(self):
        seen = {
            "/path/sess-1/file.txt": 100.0,
            "/path/sess-2/file.txt": 200.0,
            "/path/sess-1/other.txt": 300.0,
        }
        clear_seen_mtimes_for_session("sess-1", seen)
        assert "/path/sess-1/file.txt" not in seen
        assert "/path/sess-1/other.txt" not in seen
        assert "/path/sess-2/file.txt" in seen

    def test_noop_when_no_match(self):
        seen = {"/path/sess-2/file.txt": 200.0}
        clear_seen_mtimes_for_session("sess-99", seen)
        assert len(seen) == 1

    def test_empty_dict(self):
        seen: dict[str, float] = {}
        clear_seen_mtimes_for_session("sess-1", seen)
        assert len(seen) == 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
