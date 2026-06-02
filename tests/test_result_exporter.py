"""Tests for ResultExporterService."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.result_exporter import ResultExporterService


def _make_settings(**overrides):
    """Create a mock settings object with export defaults."""
    settings = MagicMock()
    settings.auto_export_threshold_chars = overrides.get("auto_export_threshold_chars", 4096)
    settings.zip_max_size_mb = overrides.get("zip_max_size_mb", 50)
    return settings


def _make_task_record(
    task_id: str = "task-123",
    provider: str = "claude_code",
    prompt: str = "Fix the bug",
    workdir: str = "/tmp/work",
    status: str = "SUCCEEDED",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
):
    """Create a mock TaskRecord."""
    record = MagicMock()
    record.task_id = task_id
    record.provider = provider
    record.prompt = prompt
    record.workdir = workdir
    record.status = status
    record.created_at = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
    record.started_at = started_at or datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
    record.ended_at = ended_at or datetime(2025, 1, 15, 10, 35, 0, tzinfo=UTC)
    record.duration_sec = 300.0
    return record


@pytest.fixture
def settings():
    return _make_settings()


@pytest.fixture
def service(settings):
    return ResultExporterService(settings=settings)


class TestFormatMarkdownHeader:
    def test_contains_task_id(self, service: ResultExporterService) -> None:
        result = service.format_markdown_header(
            task_id="abc-123",
            provider="claude_code",
            duration_sec=45.2,
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
        )
        assert "abc-123" in result

    def test_contains_provider(self, service: ResultExporterService) -> None:
        result = service.format_markdown_header(
            task_id="t1",
            provider="gemini",
            duration_sec=10.0,
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
        )
        assert "gemini" in result

    def test_contains_duration(self, service: ResultExporterService) -> None:
        result = service.format_markdown_header(
            task_id="t1",
            provider="claude_code",
            duration_sec=123.4,
            timestamp=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert "123.4s" in result

    def test_duration_none_shows_na(self, service: ResultExporterService) -> None:
        result = service.format_markdown_header(
            task_id="t1",
            provider="claude_code",
            duration_sec=None,
            timestamp=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert "N/A" in result

    def test_contains_timestamp(self, service: ResultExporterService) -> None:
        result = service.format_markdown_header(
            task_id="t1",
            provider="claude_code",
            duration_sec=5.0,
            timestamp=datetime(2025, 3, 20, 14, 30, 45, tzinfo=UTC),
        )
        assert "2025-03-20 14:30:45 UTC" in result


class TestShouldAutoExport:
    def test_below_threshold_returns_false(self, service: ResultExporterService) -> None:
        assert service.should_auto_export(4096) is False

    def test_above_threshold_returns_true(self, service: ResultExporterService) -> None:
        assert service.should_auto_export(4097) is True

    def test_zero_returns_false(self, service: ResultExporterService) -> None:
        assert service.should_auto_export(0) is False

    def test_exactly_at_threshold_returns_false(self, service: ResultExporterService) -> None:
        assert service.should_auto_export(4096) is False


class TestCollectModifiedFiles:
    def test_collects_files_within_time_range(self, service: ResultExporterService, tmp_path: Path) -> None:
        # Create a file with mtime in range
        f = tmp_path / "modified.txt"
        f.write_text("hello")
        now = time.time()
        os.utime(f, (now, now))

        started_at = datetime.fromtimestamp(now - 10, tz=UTC)
        ended_at = datetime.fromtimestamp(now + 10, tz=UTC)

        result = service.collect_modified_files(
            workdir=str(tmp_path),
            started_at=started_at,
            ended_at=ended_at,
            gitignore_patterns=[],
        )
        assert f in result

    def test_excludes_files_outside_time_range(self, service: ResultExporterService, tmp_path: Path) -> None:
        f = tmp_path / "old.txt"
        f.write_text("old content")
        old_time = time.time() - 3600
        os.utime(f, (old_time, old_time))

        now = time.time()
        started_at = datetime.fromtimestamp(now - 10, tz=UTC)
        ended_at = datetime.fromtimestamp(now + 10, tz=UTC)

        result = service.collect_modified_files(
            workdir=str(tmp_path),
            started_at=started_at,
            ended_at=ended_at,
            gitignore_patterns=[],
        )
        assert f not in result

    def test_excludes_gitignored_files(self, service: ResultExporterService, tmp_path: Path) -> None:
        f = tmp_path / "node_modules" / "pkg.js"
        f.parent.mkdir()
        f.write_text("module")
        now = time.time()
        os.utime(f, (now, now))

        started_at = datetime.fromtimestamp(now - 10, tz=UTC)
        ended_at = datetime.fromtimestamp(now + 10, tz=UTC)

        result = service.collect_modified_files(
            workdir=str(tmp_path),
            started_at=started_at,
            ended_at=ended_at,
            gitignore_patterns=["node_modules"],
        )
        assert f not in result

    def test_excludes_by_extension_pattern(self, service: ResultExporterService, tmp_path: Path) -> None:
        f = tmp_path / "output.log"
        f.write_text("log data")
        now = time.time()
        os.utime(f, (now, now))

        started_at = datetime.fromtimestamp(now - 10, tz=UTC)
        ended_at = datetime.fromtimestamp(now + 10, tz=UTC)

        result = service.collect_modified_files(
            workdir=str(tmp_path),
            started_at=started_at,
            ended_at=ended_at,
            gitignore_patterns=["*.log"],
        )
        assert f not in result

    def test_nonexistent_workdir_returns_empty(self, service: ResultExporterService) -> None:
        result = service.collect_modified_files(
            workdir="/nonexistent/path",
            started_at=datetime(2025, 1, 1, tzinfo=UTC),
            ended_at=datetime(2025, 1, 2, tzinfo=UTC),
            gitignore_patterns=[],
        )
        assert result == []

    def test_returns_sorted_results(self, service: ResultExporterService, tmp_path: Path) -> None:
        now = time.time()
        files = []
        for name in ["z.txt", "a.txt", "m.txt"]:
            f = tmp_path / name
            f.write_text("content")
            os.utime(f, (now, now))
            files.append(f)

        started_at = datetime.fromtimestamp(now - 10, tz=UTC)
        ended_at = datetime.fromtimestamp(now + 10, tz=UTC)

        result = service.collect_modified_files(
            workdir=str(tmp_path),
            started_at=started_at,
            ended_at=ended_at,
            gitignore_patterns=[],
        )
        assert result == sorted(result)


class TestExportMarkdown:
    def test_creates_markdown_file(self, service: ResultExporterService) -> None:
        record = _make_task_record()
        result = asyncio.run(service.export_markdown(record))
        assert result.file_path.exists()
        assert result.filename == "task_task-123.md"
        assert result.mime_type == "text/markdown"

    def test_markdown_contains_header(self, service: ResultExporterService) -> None:
        record = _make_task_record(task_id="xyz-789", provider="gemini")
        result = asyncio.run(service.export_markdown(record))
        content = result.file_path.read_text(encoding="utf-8")
        assert "xyz-789" in content
        assert "gemini" in content


class TestExportZip:
    def test_creates_zip_file(self, service: ResultExporterService, tmp_path: Path) -> None:
        now = time.time()
        f = tmp_path / "code.py"
        f.write_text("print('hello')")
        os.utime(f, (now, now))

        record = _make_task_record(task_id="z1")
        started_at = datetime.fromtimestamp(now - 10, tz=UTC)
        ended_at = datetime.fromtimestamp(now + 10, tz=UTC)

        result = asyncio.run(service.export_zip(record, workdir=str(tmp_path), started_at=started_at, ended_at=ended_at))
        assert result.file_path.exists()
        assert result.filename == "task_z1.zip"
        assert result.mime_type == "application/zip"

    def test_zip_contains_markdown_and_modified_files(self, service: ResultExporterService, tmp_path: Path) -> None:
        import zipfile

        now = time.time()
        f = tmp_path / "result.txt"
        f.write_text("output data")
        os.utime(f, (now, now))

        record = _make_task_record(task_id="z2")
        started_at = datetime.fromtimestamp(now - 10, tz=UTC)
        ended_at = datetime.fromtimestamp(now + 10, tz=UTC)

        result = asyncio.run(service.export_zip(record, workdir=str(tmp_path), started_at=started_at, ended_at=ended_at))

        with zipfile.ZipFile(result.file_path, "r") as zf:
            names = zf.namelist()
            assert "task_z2.md" in names
            assert "result.txt" in names

    def test_zip_excludes_gitignored_files(self, service: ResultExporterService, tmp_path: Path) -> None:
        import zipfile

        now = time.time()

        # Create .gitignore
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n__pycache__\n")
        os.utime(gitignore, (now, now))

        # Create an ignored file
        ignored = tmp_path / "module.pyc"
        ignored.write_text("bytecode")
        os.utime(ignored, (now, now))

        # Create an included file
        included = tmp_path / "main.py"
        included.write_text("print('ok')")
        os.utime(included, (now, now))

        record = _make_task_record(task_id="z3")
        started_at = datetime.fromtimestamp(now - 10, tz=UTC)
        ended_at = datetime.fromtimestamp(now + 10, tz=UTC)

        result = asyncio.run(service.export_zip(record, workdir=str(tmp_path), started_at=started_at, ended_at=ended_at))

        with zipfile.ZipFile(result.file_path, "r") as zf:
            names = zf.namelist()
            assert "main.py" in names
            assert "module.pyc" not in names
