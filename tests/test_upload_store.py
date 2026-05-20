"""Tests for UploadStoreAdapter."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.adapters.storage.upload_store import UploadStoreAdapter


@pytest.fixture
def tmp_workdir(tmp_path: Path) -> str:
    return str(tmp_path / "workdir")


@pytest.fixture
def adapter(tmp_path: Path) -> UploadStoreAdapter:
    base = tmp_path / "workdir"
    base.mkdir()
    return UploadStoreAdapter(str(base))


class TestUserUploadDir:
    def test_returns_correct_path(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        workdir = str(tmp_path / "workdir")
        result = adapter.user_upload_dir(123, workdir)
        assert result == Path(workdir) / ".tg-uploads" / "123"

    def test_different_users_different_dirs(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        workdir = str(tmp_path / "workdir")
        assert adapter.user_upload_dir(1, workdir) != adapter.user_upload_dir(2, workdir)


class TestDeduplicateFilename:
    def test_no_collision(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        target = tmp_path / "uploads"
        target.mkdir()
        assert adapter.deduplicate_filename(target, "test.py") == "test.py"

    def test_single_collision(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        target = tmp_path / "uploads"
        target.mkdir()
        (target / "test.py").write_text("x")
        assert adapter.deduplicate_filename(target, "test.py") == "test_1.py"

    def test_multiple_collisions(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        target = tmp_path / "uploads"
        target.mkdir()
        (target / "test.py").write_text("x")
        (target / "test_1.py").write_text("x")
        (target / "test_2.py").write_text("x")
        assert adapter.deduplicate_filename(target, "test.py") == "test_3.py"

    def test_no_extension(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        target = tmp_path / "uploads"
        target.mkdir()
        (target / "Makefile").write_text("x")
        assert adapter.deduplicate_filename(target, "Makefile") == "Makefile_1"


class TestSaveFile:
    def test_saves_file(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        workdir = str(tmp_path / "workdir")
        path = asyncio.run(adapter.save_file(42, workdir, "hello.txt", b"content"))
        assert path.exists()
        assert path.read_bytes() == b"content"
        assert path.name == "hello.txt"

    def test_deduplicates_on_collision(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        workdir = str(tmp_path / "workdir")
        p1 = asyncio.run(adapter.save_file(42, workdir, "f.txt", b"a"))
        p2 = asyncio.run(adapter.save_file(42, workdir, "f.txt", b"b"))
        assert p1.name == "f.txt"
        assert p2.name == "f_1.txt"
        assert p1.read_bytes() == b"a"
        assert p2.read_bytes() == b"b"


class TestCollectPendingFiles:
    def test_collects_files_after_since(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        workdir = str(tmp_path / "workdir")
        upload_dir = adapter.user_upload_dir(1, workdir)
        upload_dir.mkdir(parents=True)

        # Create a file with old mtime
        old_file = upload_dir / "old.txt"
        old_file.write_text("old")
        old_mtime = time.time() - 3600
        os.utime(old_file, (old_mtime, old_mtime))

        # Create a file with current mtime
        new_file = upload_dir / "new.txt"
        new_file.write_text("new")

        # Use a timestamp before old_file — should get both
        all_files = adapter.collect_pending_files(1, workdir, datetime.fromtimestamp(0, tz=timezone.utc))
        assert len(all_files) == 2

        # With since after old_file — should get only new
        since = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc)
        recent = adapter.collect_pending_files(1, workdir, since)
        assert len(recent) == 1
        assert recent[0].name == "new.txt"

    def test_returns_empty_for_nonexistent_dir(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        workdir = str(tmp_path / "workdir")
        result = adapter.collect_pending_files(999, workdir, datetime.fromtimestamp(0, tz=timezone.utc))
        assert result == []


class TestClearUserFiles:
    def test_removes_directory(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        workdir = str(tmp_path / "workdir")
        upload_dir = adapter.user_upload_dir(1, workdir)
        upload_dir.mkdir(parents=True)
        (upload_dir / "test.txt").write_text("x")

        adapter.clear_user_files(1, workdir)
        assert not upload_dir.exists()

    def test_no_error_on_nonexistent(self, adapter: UploadStoreAdapter, tmp_path: Path) -> None:
        workdir = str(tmp_path / "workdir")
        adapter.clear_user_files(999, workdir)  # Should not raise


class TestCleanupExpired:
    def test_deletes_old_files(self, tmp_path: Path) -> None:
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        adapter = UploadStoreAdapter(str(workdir))

        upload_dir = workdir / ".tg-uploads" / "1"
        upload_dir.mkdir(parents=True)

        old_file = upload_dir / "old.txt"
        old_file.write_text("old")
        old_mtime = time.time() - (25 * 3600)  # 25 hours old
        os.utime(old_file, (old_mtime, old_mtime))

        new_file = upload_dir / "new.txt"
        new_file.write_text("new")

        deleted = adapter.cleanup_expired(max_age_hours=24)
        assert deleted == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_returns_zero_when_nothing_expired(self, tmp_path: Path) -> None:
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        adapter = UploadStoreAdapter(str(workdir))

        upload_dir = workdir / ".tg-uploads" / "1"
        upload_dir.mkdir(parents=True)
        (upload_dir / "fresh.txt").write_text("fresh")

        deleted = adapter.cleanup_expired(max_age_hours=24)
        assert deleted == 0
