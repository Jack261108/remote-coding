"""Service for capturing filesystem snapshots and generating unified diffs."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.domain.file_models import DiffResult

_PATCH_FILE_THRESHOLD = 4096
_DEFAULT_MAX_SNAPSHOT_FILE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    mtime: float
    size: int
    digest: str | None
    content: str | None


class DiffGeneratorService:
    def __init__(self, *, max_snapshot_file_bytes: int = _DEFAULT_MAX_SNAPSHOT_FILE_BYTES) -> None:
        self._max_snapshot_file_bytes = max_snapshot_file_bytes

    def capture_snapshot(self, workdir: str, gitignore_patterns: list[str]) -> dict[Path, SnapshotEntry]:
        """Capture metadata and bounded text content for tracked files in workdir."""
        snapshot: dict[Path, SnapshotEntry] = {}
        workdir_path = Path(workdir).resolve()

        for root, dirs, files in os.walk(workdir_path, followlinks=False):
            root_path = Path(root)
            # Filter out directories matching gitignore patterns
            dirs[:] = [d for d in dirs if not self._matches_gitignore(root_path / d, workdir_path, gitignore_patterns)]

            for fname in files:
                file_path = root_path / fname
                if not self.is_within_workdir(file_path, workdir):
                    continue
                if self._matches_gitignore(file_path, workdir_path, gitignore_patterns):
                    continue
                try:
                    file_stat = file_path.lstat()
                    if not stat.S_ISREG(file_stat.st_mode):
                        continue
                    if self.is_binary_file(file_path):
                        snapshot[file_path] = SnapshotEntry(
                            mtime=file_stat.st_mtime,
                            size=file_stat.st_size,
                            digest=None,
                            content=None,
                        )
                        continue

                    content = None
                    digest = self._digest_file(file_path)
                    if file_stat.st_size <= self._max_snapshot_file_bytes:
                        content = file_path.read_text(errors="replace")
                    snapshot[file_path] = SnapshotEntry(
                        mtime=file_stat.st_mtime,
                        size=file_stat.st_size,
                        digest=digest,
                        content=content,
                    )
                except OSError:
                    continue

        return snapshot

    def detect_modified_files(self, *, workdir: str, pre_snapshot: dict[Path, SnapshotEntry], gitignore_patterns: list[str]) -> list[Path]:
        """Compare current state against snapshot. Only includes files within workdir,
        excludes binary files and gitignored files."""
        current_snapshot = self.capture_snapshot(workdir, gitignore_patterns)
        modified: list[Path] = []

        # Check for modified or new files
        for path, entry in current_snapshot.items():
            if not self.is_within_workdir(path, workdir):
                continue
            if self.is_binary_file(path):
                continue
            previous = pre_snapshot.get(path)
            if previous is None or self._snapshot_entry_changed(previous, entry):
                modified.append(path)

        # Check for deleted text files captured before the task.
        for path, previous in pre_snapshot.items():
            if path in current_snapshot:
                continue
            if previous.content is None and previous.digest is None:
                continue
            if not self.is_within_workdir(path, workdir):
                continue
            if self._matches_gitignore(path, Path(workdir).resolve(), gitignore_patterns):
                continue
            modified.append(path)

        return sorted(modified)

    def generate_unified_diff(self, modified_files: list[Path], pre_snapshot: dict[Path, SnapshotEntry]) -> DiffResult | None:
        """Generate unified diff for modified files. Returns None if no modifications."""
        if not modified_files:
            return None

        diff_parts: list[str] = []
        files_with_diff = 0

        for file_path in modified_files:
            file_exists = file_path.exists()
            try:
                current_content = file_path.read_text(errors="replace") if file_exists else ""
            except OSError:
                continue

            current_lines = current_content.splitlines(keepends=True)
            old_entry = pre_snapshot.get(file_path)
            old_content = old_entry.content if old_entry is not None else None
            if old_entry is not None and old_content is None:
                continue
            old_lines = old_content.splitlines(keepends=True) if old_content is not None else []

            diff = difflib.unified_diff(
                old_lines,
                current_lines,
                fromfile=f"a/{file_path.name}",
                tofile=f"b/{file_path.name}" if file_exists else "/dev/null",
            )
            diff_text = "".join(diff)
            if diff_text:
                diff_parts.append(diff_text)
                files_with_diff += 1

        if not diff_parts:
            return None

        content = "\n".join(diff_parts)
        return DiffResult(
            content=content,
            file_count=files_with_diff,
            is_patch_file=len(content) >= _PATCH_FILE_THRESHOLD,
        )

    def is_within_workdir(self, file_path: Path, workdir: str) -> bool:
        """Security check: ensure file is within workdir boundaries."""
        try:
            resolved = file_path.resolve()
            workdir_resolved = Path(workdir).resolve()
            # Check that the resolved path starts with the workdir
            resolved.relative_to(workdir_resolved)
            return True
        except (ValueError, OSError):
            return False

    def is_binary_file(self, path: Path) -> bool:
        """Heuristic check for binary files (null bytes in first 8KB)."""
        try:
            with open(path, "rb") as f:
                chunk = f.read(8192)
            return b"\x00" in chunk
        except OSError:
            return False

    def _digest_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _snapshot_entry_changed(self, previous: SnapshotEntry, current: SnapshotEntry) -> bool:
        return (
            previous.mtime != current.mtime
            or previous.size != current.size
            or previous.digest != current.digest
            or previous.content != current.content
        )

    def _matches_gitignore(self, path: Path, workdir_path: Path, patterns: list[str]) -> bool:
        """Check if a path matches any gitignore pattern using fnmatch on relative path."""
        try:
            rel_path = path.relative_to(workdir_path)
        except ValueError:
            return False

        rel_str = str(rel_path)
        name = path.name

        for pattern in patterns:
            # Match against relative path and filename
            if fnmatch.fnmatch(rel_str, pattern):
                return True
            if fnmatch.fnmatch(name, pattern):
                return True
            # Support patterns like "dir/" matching directories
            if pattern.endswith("/") and fnmatch.fnmatch(rel_str, pattern.rstrip("/")):
                return True
            # Support patterns with leading slash (anchored to root)
            if pattern.startswith("/") and fnmatch.fnmatch(rel_str, pattern.lstrip("/")):
                return True
        return False
