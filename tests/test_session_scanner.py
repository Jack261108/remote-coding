"""Tests for SessionScanner service."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from app.adapters.claude.paths import ClaudePaths
from app.services.session_scanner import SessionScanner


@pytest.fixture
def scanner() -> SessionScanner:
    return SessionScanner()


@pytest.fixture
def claude_paths(tmp_path: Path) -> ClaudePaths:
    return ClaudePaths(root_dir=tmp_path / ".claude")


def _make_session_file(
    projects_dir: Path,
    encoded_workdir: str,
    session_id: str,
    human_message: str = "hello world",
    mtime: float | None = None,
) -> Path:
    session_dir = projects_dir / encoded_workdir
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{session_id}.jsonl"

    lines = [
        json.dumps({"type": "permission-mode", "sessionId": session_id}),
        json.dumps({"type": "human", "message": {"content": [{"type": "text", "text": human_message}]}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))

    return path


class TestEncodeWorkdir:
    def test_replaces_slashes(self, scanner: SessionScanner) -> None:
        assert scanner.encode_workdir("/home/user/project") == "-home-user-project"

    def test_root_path(self, scanner: SessionScanner) -> None:
        assert scanner.encode_workdir("/") == "-"

    def test_no_slashes(self, scanner: SessionScanner) -> None:
        assert scanner.encode_workdir("relative") == "relative"


class TestScan:
    def test_returns_empty_when_dir_missing(self, scanner: SessionScanner, claude_paths: ClaudePaths) -> None:
        result = scanner.scan("/nonexistent/path", claude_paths)
        assert result == []

    def test_discovers_session_files(self, scanner: SessionScanner, claude_paths: ClaudePaths) -> None:
        encoded = scanner.encode_workdir("/home/user/project")
        _make_session_file(claude_paths.projects_dir, encoded, "abc-123", "hi there")

        result = scanner.scan("/home/user/project", claude_paths)
        assert len(result) == 1
        assert result[0].session_id == "abc-123"
        assert result[0].summary == "hi there"
        assert isinstance(result[0].modified_at, datetime)

    def test_excludes_non_jsonl_files(self, scanner: SessionScanner, claude_paths: ClaudePaths) -> None:
        encoded = scanner.encode_workdir("/work")
        session_dir = claude_paths.projects_dir / encoded
        session_dir.mkdir(parents=True)
        (session_dir / "readme.txt").write_text("ignore me")
        _make_session_file(claude_paths.projects_dir, encoded, "s1")

        result = scanner.scan("/work", claude_paths)
        assert len(result) == 1
        assert result[0].session_id == "s1"

    def test_excludes_subagents_directory(self, scanner: SessionScanner, claude_paths: ClaudePaths) -> None:
        encoded = scanner.encode_workdir("/work")
        session_dir = claude_paths.projects_dir / encoded
        subagents = session_dir / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent-1.jsonl").write_text(json.dumps({"type": "human", "message": {"content": "sub"}}) + "\n")
        _make_session_file(claude_paths.projects_dir, encoded, "main-session")

        result = scanner.scan("/work", claude_paths)
        assert len(result) == 1
        assert result[0].session_id == "main-session"

    def test_sorts_by_mtime_descending(self, scanner: SessionScanner, claude_paths: ClaudePaths) -> None:
        encoded = scanner.encode_workdir("/work")
        now = time.time()
        _make_session_file(claude_paths.projects_dir, encoded, "old", "old msg", mtime=now - 100)
        _make_session_file(claude_paths.projects_dir, encoded, "new", "new msg", mtime=now)
        _make_session_file(claude_paths.projects_dir, encoded, "mid", "mid msg", mtime=now - 50)

        result = scanner.scan("/work", claude_paths)
        assert [s.session_id for s in result] == ["new", "mid", "old"]

    def test_respects_max_results(self, scanner: SessionScanner, claude_paths: ClaudePaths) -> None:
        encoded = scanner.encode_workdir("/work")
        for i in range(15):
            _make_session_file(claude_paths.projects_dir, encoded, f"session-{i:02d}", mtime=float(i))

        result = scanner.scan("/work", claude_paths, max_results=10)
        assert len(result) == 10

    def test_extracts_string_content(self, scanner: SessionScanner, claude_paths: ClaudePaths) -> None:
        encoded = scanner.encode_workdir("/work")
        session_dir = claude_paths.projects_dir / encoded
        session_dir.mkdir(parents=True)
        path = session_dir / "s1.jsonl"
        path.write_text(json.dumps({"type": "human", "message": {"content": "plain string content"}}) + "\n")

        result = scanner.scan("/work", claude_paths)
        assert result[0].summary == "plain string content"

    def test_skips_malformed_jsonl(self, scanner: SessionScanner, claude_paths: ClaudePaths) -> None:
        encoded = scanner.encode_workdir("/work")
        session_dir = claude_paths.projects_dir / encoded
        session_dir.mkdir(parents=True)
        path = session_dir / "bad.jsonl"
        path.write_text("not valid json\n")

        result = scanner.scan("/work", claude_paths)
        assert len(result) == 1
        assert result[0].summary == ""
