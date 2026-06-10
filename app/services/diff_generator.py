"""Service for capturing filesystem snapshots and generating unified diffs."""

from __future__ import annotations

import difflib
import fnmatch
import os
from pathlib import Path

from app.domain.file_models import DiffResult

_PATCH_FILE_THRESHOLD = 4096
SnapshotEntry = tuple[float, str | None]


class DiffGeneratorService:
    def __init__(self) -> None:
        pass

    def capture_snapshot(self, workdir: str, gitignore_patterns: list[str]) -> dict[Path, SnapshotEntry]:
        """Capture mtime and text content of all tracked files in workdir, respecting gitignore patterns."""
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
                    stat = file_path.lstat()
                    content = None if self.is_binary_file(file_path) else file_path.read_text(errors="replace")
                    snapshot[file_path] = (stat.st_mtime, content)
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
            # New file or modified file
            if path not in pre_snapshot or entry[0] != pre_snapshot[path][0]:
                modified.append(path)

        return sorted(modified)

    def generate_unified_diff(self, modified_files: list[Path], pre_snapshot: dict[Path, SnapshotEntry]) -> DiffResult | None:
        """Generate unified diff for modified files. Returns None if no modifications."""
        if not modified_files:
            return None

        diff_parts: list[str] = []
        files_with_diff = 0

        for file_path in modified_files:
            try:
                current_content = file_path.read_text(errors="replace")
            except OSError:
                continue

            current_lines = current_content.splitlines(keepends=True)

            if file_path in pre_snapshot:
                old_content = pre_snapshot[file_path][1]
                old_lines = old_content.splitlines(keepends=True) if old_content is not None else []
            else:
                old_lines = []

            diff = difflib.unified_diff(
                old_lines,
                current_lines,
                fromfile=f"a/{file_path.name}",
                tofile=f"b/{file_path.name}",
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
