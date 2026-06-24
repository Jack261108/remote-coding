"""Unit tests for file_mtime_utils."""

from __future__ import annotations

import time

import pytest

from app.infra.file_mtime_utils import clear_seen_mtimes_for_session, refresh_seen_mtimes

pytestmark = pytest.mark.asyncio

# -- refresh_seen_mtimes --


class TestRefreshSeenMtimes:
    async def test_new_file_detected(self, tmp_path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("hello")
        seen: dict[str, float] = {}
        result = await refresh_seen_mtimes({str(f)}, seen)
        assert str(f) in result
        assert seen[str(f)] == result[str(f)]

    async def test_unchanged_file_not_in_result(self, tmp_path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("hello")
        seen: dict[str, float] = {}
        await refresh_seen_mtimes({str(f)}, seen)
        result = await refresh_seen_mtimes({str(f)}, seen)
        assert str(f) not in result

    async def test_modified_file_detected(self, tmp_path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("v1")
        seen: dict[str, float] = {}
        await refresh_seen_mtimes({str(f)}, seen)
        # Ensure mtime changes
        time.sleep(0.05)
        f.write_text("v2")
        result = await refresh_seen_mtimes({str(f)}, seen)
        assert str(f) in result
        assert seen[str(f)] == result[str(f)]

    async def test_nonexistent_file_skipped(self, tmp_path) -> None:
        seen: dict[str, float] = {}
        result = await refresh_seen_mtimes({str(tmp_path / "nonexistent.txt")}, seen)
        assert result == {}
        assert str(tmp_path / "nonexistent.txt") not in seen

    async def test_empty_paths(self) -> None:
        seen: dict[str, float] = {}
        result = await refresh_seen_mtimes(set(), seen)
        assert result == {}

    async def test_multiple_files(self, tmp_path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a")
        f2.write_text("b")
        seen: dict[str, float] = {}
        result = await refresh_seen_mtimes({str(f1), str(f2)}, seen)
        assert len(result) == 2
        assert str(f1) in seen
        assert str(f2) in seen

    async def test_mixed_existing_and_new(self, tmp_path) -> None:
        f1 = tmp_path / "old.txt"
        f2 = tmp_path / "new.txt"
        f1.write_text("old")
        f2.write_text("new")
        seen: dict[str, float] = {}
        await refresh_seen_mtimes({str(f1)}, seen)
        result = await refresh_seen_mtimes({str(f1), str(f2)}, seen)
        assert str(f1) not in result
        assert str(f2) in result

    async def test_seen_mtimes_mutated_in_place(self, tmp_path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("content")
        seen: dict[str, float] = {}
        await refresh_seen_mtimes({str(f)}, seen)
        assert len(seen) == 1


# -- clear_seen_mtimes_for_session --


class TestClearSeenMtimesForSession:
    async def test_removes_matching_keys(self) -> None:
        seen = {
            "session_abc/file1.txt": 1.0,
            "session_abc/file2.txt": 2.0,
            "session_xyz/file3.txt": 3.0,
        }
        await clear_seen_mtimes_for_session("session_abc", seen)
        assert "session_abc/file1.txt" not in seen
        assert "session_abc/file2.txt" not in seen
        assert "session_xyz/file3.txt" in seen

    async def test_no_match_no_removal(self) -> None:
        seen = {"a.txt": 1.0, "b.txt": 2.0}
        await clear_seen_mtimes_for_session("nonexistent", seen)
        assert len(seen) == 2

    async def test_empty_cache(self) -> None:
        seen: dict[str, float] = {}
        await clear_seen_mtimes_for_session("any", seen)  # should not raise

    async def test_session_id_substring_match(self) -> None:
        seen = {
            "abc_session_xyz/file.txt": 1.0,
            "other/file.txt": 2.0,
        }
        await clear_seen_mtimes_for_session("session", seen)
        assert "abc_session_xyz/file.txt" not in seen
        assert "other/file.txt" in seen

    async def test_exact_key_match(self) -> None:
        seen = {"session_abc": 1.0}
        await clear_seen_mtimes_for_session("session_abc", seen)
        assert len(seen) == 0
