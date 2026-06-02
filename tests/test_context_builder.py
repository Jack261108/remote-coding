"""Tests for ContextBuilderService."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from app.adapters.storage.upload_store import UploadStoreAdapter
from app.services.context_builder import ContextBuilderService


@pytest.fixture
def workdir(tmp_path: Path) -> str:
    d = tmp_path / "workdir"
    d.mkdir(exist_ok=True)
    return str(d)


@pytest.fixture
def adapter(workdir: str) -> UploadStoreAdapter:
    return UploadStoreAdapter(workdir)


@pytest.fixture
def service(adapter: UploadStoreAdapter) -> ContextBuilderService:
    return ContextBuilderService(upload_store=adapter)


class TestBuildCliArgs:
    def test_claude_code_produces_file_flags(self, service: ContextBuilderService, tmp_path: Path) -> None:
        paths = [tmp_path / "a.py", tmp_path / "b.txt"]
        result = service.build_cli_args("claude_code", paths)
        assert result == ["--file", str(paths[0]), "--file", str(paths[1])]

    def test_claude_code_empty_paths(self, service: ContextBuilderService) -> None:
        result = service.build_cli_args("claude_code", [])
        assert result == []

    def test_codex_returns_empty(self, service: ContextBuilderService, tmp_path: Path) -> None:
        paths = [tmp_path / "a.py"]
        result = service.build_cli_args("codex", paths)
        assert result == []

    def test_gemini_returns_empty(self, service: ContextBuilderService, tmp_path: Path) -> None:
        paths = [tmp_path / "a.py"]
        result = service.build_cli_args("gemini", paths)
        assert result == []

    def test_unknown_provider_returns_empty(self, service: ContextBuilderService, tmp_path: Path) -> None:
        paths = [tmp_path / "a.py"]
        result = service.build_cli_args("unknown_provider", paths)
        assert result == []


class TestAugmentPrompt:
    def test_appends_file_summary(self, service: ContextBuilderService, tmp_path: Path) -> None:
        paths = [tmp_path / "code.py", tmp_path / "data.json"]
        result = service.augment_prompt("Fix the bug", paths)
        assert result.startswith("Fix the bug")
        assert "code.py" in result
        assert "data.json" in result
        assert "[Attached files:" in result

    def test_empty_paths_returns_original(self, service: ContextBuilderService) -> None:
        result = service.augment_prompt("Hello", [])
        assert result == "Hello"

    def test_preserves_original_prompt(self, service: ContextBuilderService, tmp_path: Path) -> None:
        prompt = "A complex prompt\nwith newlines"
        paths = [tmp_path / "f.txt"]
        result = service.augment_prompt(prompt, paths)
        assert result.startswith(prompt)


class TestBuildContext:
    def test_no_files_returns_original_prompt(self, service: ContextBuilderService, adapter: UploadStoreAdapter, workdir: str) -> None:
        since = datetime.fromtimestamp(0, tz=UTC)
        ctx = service.build_context(user_id=1, workdir=workdir, provider="claude_code", prompt="Do stuff", since=since)
        assert ctx.file_paths == []
        assert ctx.augmented_prompt == "Do stuff"
        assert ctx.cli_args == []

    def test_with_files_claude_code(self, service: ContextBuilderService, adapter: UploadStoreAdapter, workdir: str) -> None:
        # Upload a file
        asyncio.run(adapter.save_file(1, workdir, "test.py", b"content"))

        since = datetime.fromtimestamp(0, tz=UTC)
        ctx = service.build_context(user_id=1, workdir=workdir, provider="claude_code", prompt="Fix it", since=since)
        assert len(ctx.file_paths) == 1
        assert ctx.file_paths[0].name == "test.py"
        assert "--file" in ctx.cli_args
        assert str(ctx.file_paths[0]) in ctx.cli_args
        assert "test.py" in ctx.augmented_prompt

    def test_with_files_codex(self, service: ContextBuilderService, adapter: UploadStoreAdapter, workdir: str) -> None:
        asyncio.run(adapter.save_file(1, workdir, "data.json", b"{}"))

        since = datetime.fromtimestamp(0, tz=UTC)
        ctx = service.build_context(user_id=1, workdir=workdir, provider="codex", prompt="Analyze", since=since)
        assert len(ctx.file_paths) == 1
        assert ctx.cli_args == []
        assert "data.json" in ctx.augmented_prompt

    def test_respects_since_timestamp(self, service: ContextBuilderService, adapter: UploadStoreAdapter, workdir: str) -> None:
        # Create a file with old mtime
        upload_dir = adapter.user_upload_dir(1, workdir)
        upload_dir.mkdir(parents=True)
        old_file = upload_dir / "old.txt"
        old_file.write_text("old")
        old_mtime = time.time() - 3600
        os.utime(old_file, (old_mtime, old_mtime))

        # Since is after old file
        since = datetime.fromtimestamp(time.time() - 60, tz=UTC)
        ctx = service.build_context(user_id=1, workdir=workdir, provider="claude_code", prompt="Go", since=since)
        assert ctx.file_paths == []
        assert ctx.augmented_prompt == "Go"


class TestCleanupAfterTask:
    def test_clears_user_files(self, service: ContextBuilderService, adapter: UploadStoreAdapter, workdir: str) -> None:
        asyncio.run(adapter.save_file(1, workdir, "f.txt", b"data"))
        upload_dir = adapter.user_upload_dir(1, workdir)
        assert upload_dir.exists()

        asyncio.run(service.cleanup_after_task(1, workdir))
        assert not upload_dir.exists()

    def test_logs_on_failure(self, service: ContextBuilderService, workdir: str) -> None:
        with patch.object(service._upload_store, "clear_user_files", side_effect=OSError("permission denied")):
            # Should not raise
            asyncio.run(service.cleanup_after_task(1, workdir))

    def test_no_error_on_nonexistent_user(self, service: ContextBuilderService, workdir: str) -> None:
        # Should not raise for non-existent user dir
        asyncio.run(service.cleanup_after_task(999, workdir))
