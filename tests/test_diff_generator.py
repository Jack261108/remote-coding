"""Tests for DiffGeneratorService."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.services.diff_generator import DiffGeneratorService


@pytest.fixture
def workdir(tmp_path: Path) -> str:
    d = tmp_path / "workdir"
    d.mkdir(exist_ok=True)
    return str(d)


@pytest.fixture
def service() -> DiffGeneratorService:
    return DiffGeneratorService()


class TestIsWithinWorkdir:
    def test_file_inside_workdir(self, service: DiffGeneratorService, workdir: str) -> None:
        p = Path(workdir) / "sub" / "file.py"
        assert service.is_within_workdir(p, workdir) is True

    def test_file_at_workdir_root(self, service: DiffGeneratorService, workdir: str) -> None:
        p = Path(workdir) / "file.py"
        assert service.is_within_workdir(p, workdir) is True

    def test_file_outside_workdir(self, service: DiffGeneratorService, workdir: str) -> None:
        p = Path(workdir).parent / "other" / "file.py"
        assert service.is_within_workdir(p, workdir) is False

    def test_path_traversal_attack(self, service: DiffGeneratorService, workdir: str) -> None:
        p = Path(workdir) / ".." / "etc" / "passwd"
        assert service.is_within_workdir(p, workdir) is False

    def test_symlink_outside_workdir(self, service: DiffGeneratorService, workdir: str, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "secret.txt"
        target.write_text("secret")
        link = Path(workdir) / "link.txt"
        link.symlink_to(target)
        assert service.is_within_workdir(link, workdir) is False


class TestIsBinaryFile:
    def test_text_file(self, service: DiffGeneratorService, tmp_path: Path) -> None:
        f = tmp_path / "text.py"
        f.write_text("print('hello')")
        assert service.is_binary_file(f) is False

    def test_binary_file_with_null_bytes(self, service: DiffGeneratorService, tmp_path: Path) -> None:
        f = tmp_path / "binary.bin"
        f.write_bytes(b"header\x00\x01\x02data")
        assert service.is_binary_file(f) is True

    def test_empty_file(self, service: DiffGeneratorService, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        assert service.is_binary_file(f) is False

    def test_nonexistent_file(self, service: DiffGeneratorService, tmp_path: Path) -> None:
        f = tmp_path / "missing.txt"
        assert service.is_binary_file(f) is False

    def test_null_byte_beyond_8kb(self, service: DiffGeneratorService, tmp_path: Path) -> None:
        f = tmp_path / "large.txt"
        f.write_bytes(b"A" * 8192 + b"\x00more")
        assert service.is_binary_file(f) is False


class TestCaptureSnapshot:
    def test_captures_all_files(self, service: DiffGeneratorService, workdir: str) -> None:
        Path(workdir, "a.py").write_text("a")
        Path(workdir, "b.txt").write_text("b")
        snapshot = service.capture_snapshot(workdir, [])
        assert len(snapshot) == 2

    def test_excludes_gitignored_files(self, service: DiffGeneratorService, workdir: str) -> None:
        Path(workdir, "keep.py").write_text("keep")
        Path(workdir, "skip.log").write_text("log")
        snapshot = service.capture_snapshot(workdir, ["*.log"])
        assert len(snapshot) == 1
        paths = [p.name for p in snapshot]
        assert "keep.py" in paths
        assert "skip.log" not in paths

    def test_excludes_gitignored_directories(self, service: DiffGeneratorService, workdir: str) -> None:
        sub = Path(workdir, "node_modules")
        sub.mkdir()
        (sub / "pkg.js").write_text("x")
        Path(workdir, "app.py").write_text("y")
        snapshot = service.capture_snapshot(workdir, ["node_modules"])
        paths = [p.name for p in snapshot]
        assert "app.py" in paths
        assert "pkg.js" not in paths

    def test_empty_workdir(self, service: DiffGeneratorService, workdir: str) -> None:
        snapshot = service.capture_snapshot(workdir, [])
        assert snapshot == {}

    def test_does_not_follow_symlinks_outside(self, service: DiffGeneratorService, workdir: str, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = Path(workdir) / "link_dir"
        link.symlink_to(outside)
        snapshot = service.capture_snapshot(workdir, [])
        # os.walk with followlinks=False won't follow symlinked dirs
        paths = [p.name for p in snapshot]
        assert "secret.txt" not in paths

    def test_large_text_file_content_is_not_kept_in_snapshot(self, workdir: str) -> None:
        service = DiffGeneratorService(max_snapshot_file_bytes=4)
        large_file = Path(workdir, "large.py")
        large_file.write_text("line1\nline2\n")

        snapshot = service.capture_snapshot(workdir, [])

        assert snapshot[large_file].content is None
        assert snapshot[large_file].digest is not None


class TestDetectModifiedFiles:
    def test_detects_new_file(self, service: DiffGeneratorService, workdir: str) -> None:
        snapshot = service.capture_snapshot(workdir, [])
        Path(workdir, "new.py").write_text("new content")
        modified = service.detect_modified_files(workdir=workdir, pre_snapshot=snapshot, gitignore_patterns=[])
        assert len(modified) == 1
        assert modified[0].name == "new.py"

    def test_detects_modified_file(self, service: DiffGeneratorService, workdir: str) -> None:
        f = Path(workdir, "mod.py")
        f.write_text("original")
        snapshot = service.capture_snapshot(workdir, [])
        # Ensure mtime changes
        time.sleep(0.05)
        f.write_text("modified")
        modified = service.detect_modified_files(workdir=workdir, pre_snapshot=snapshot, gitignore_patterns=[])
        assert len(modified) == 1
        assert modified[0].name == "mod.py"

    def test_excludes_binary_files(self, service: DiffGeneratorService, workdir: str) -> None:
        snapshot = service.capture_snapshot(workdir, [])
        Path(workdir, "binary.bin").write_bytes(b"\x00binary")
        modified = service.detect_modified_files(workdir=workdir, pre_snapshot=snapshot, gitignore_patterns=[])
        assert len(modified) == 0

    def test_excludes_gitignored_files(self, service: DiffGeneratorService, workdir: str) -> None:
        snapshot = service.capture_snapshot(workdir, [])
        Path(workdir, "build.log").write_text("log output")
        modified = service.detect_modified_files(workdir=workdir, pre_snapshot=snapshot, gitignore_patterns=["*.log"])
        assert len(modified) == 0

    def test_no_changes_returns_empty(self, service: DiffGeneratorService, workdir: str) -> None:
        Path(workdir, "stable.py").write_text("unchanged")
        snapshot = service.capture_snapshot(workdir, [])
        modified = service.detect_modified_files(workdir=workdir, pre_snapshot=snapshot, gitignore_patterns=[])
        assert modified == []

    def test_detects_text_change_when_mtime_is_preserved(self, service: DiffGeneratorService, workdir: str) -> None:
        f = Path(workdir, "same_mtime.py")
        f.write_text("alpha\n")
        snapshot = service.capture_snapshot(workdir, [])
        old_stat = f.stat()
        f.write_text("bravo\n")
        os.utime(f, (old_stat.st_atime, old_stat.st_mtime))

        modified = service.detect_modified_files(workdir=workdir, pre_snapshot=snapshot, gitignore_patterns=[])

        assert modified == [f]

    def test_detects_deleted_text_file(self, service: DiffGeneratorService, workdir: str) -> None:
        f = Path(workdir, "removed.py")
        f.write_text("gone\n")
        snapshot = service.capture_snapshot(workdir, [])
        f.unlink()

        modified = service.detect_modified_files(workdir=workdir, pre_snapshot=snapshot, gitignore_patterns=[])

        assert modified == [f]


class TestGenerateUnifiedDiff:
    def test_returns_none_for_empty_list(self, service: DiffGeneratorService) -> None:
        result = service.generate_unified_diff([], {})
        assert result is None

    def test_generates_diff_for_new_file(self, service: DiffGeneratorService, workdir: str) -> None:
        f = Path(workdir, "new.py")
        f.write_text("line1\nline2\n")
        result = service.generate_unified_diff([f], {})
        assert result is not None
        assert result.file_count == 1
        assert "+line1" in result.content
        assert "+line2" in result.content

    def test_generates_precise_diff_for_modified_existing_file(self, service: DiffGeneratorService, workdir: str) -> None:
        f = Path(workdir, "existing.py")
        f.write_text("line1\nline2\n")
        snapshot = service.capture_snapshot(workdir, [])
        time.sleep(0.05)
        f.write_text("line1\nchanged\n")
        modified = service.detect_modified_files(workdir=workdir, pre_snapshot=snapshot, gitignore_patterns=[])

        result = service.generate_unified_diff(modified, snapshot)

        assert result is not None
        assert " line1" in result.content
        assert "-line2" in result.content
        assert "+changed" in result.content
        assert "+line1" not in result.content

    def test_is_patch_file_below_threshold(self, service: DiffGeneratorService, workdir: str) -> None:
        f = Path(workdir, "small.py")
        f.write_text("x = 1\n")
        result = service.generate_unified_diff([f], {})
        assert result is not None
        assert result.is_patch_file is False

    def test_is_patch_file_above_threshold(self, service: DiffGeneratorService, workdir: str) -> None:
        f = Path(workdir, "large.py")
        # Generate enough content to exceed 4096 chars
        content = "\n".join(f"line_{i} = {i}" for i in range(500))
        f.write_text(content)
        result = service.generate_unified_diff([f], {})
        assert result is not None
        assert result.is_patch_file is True

    def test_multiple_files(self, service: DiffGeneratorService, workdir: str) -> None:
        f1 = Path(workdir, "a.py")
        f2 = Path(workdir, "b.py")
        f1.write_text("aaa\n")
        f2.write_text("bbb\n")
        result = service.generate_unified_diff([f1, f2], {})
        assert result is not None
        assert result.file_count == 2

    def test_unreadable_file_skipped(self, service: DiffGeneratorService, workdir: str) -> None:
        f = Path(workdir, "missing.py")
        # File doesn't exist — should be skipped gracefully
        result = service.generate_unified_diff([f], {})
        assert result is None

    def test_generates_diff_for_deleted_file(self, service: DiffGeneratorService, workdir: str) -> None:
        f = Path(workdir, "deleted.py")
        f.write_text("old\n")
        snapshot = service.capture_snapshot(workdir, [])
        f.unlink()

        result = service.generate_unified_diff([f], snapshot)

        assert result is not None
        assert result.file_count == 1
        assert "-old" in result.content
        assert "+++ /dev/null" in result.content
