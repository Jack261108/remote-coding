"""Unit tests for gitignore_utils."""

from __future__ import annotations

from pathlib import Path

from app.infra.gitignore_utils import load_gitignore_patterns

# -- 核心功能 --


class TestLoadGitignorePatterns:
    def test_loads_basic_patterns(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.pyc\n__pycache__/\n")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == ["*.pyc", "__pycache__/"]

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("  *.pyc  \n\t__pycache__/\t\n")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == ["*.pyc", "__pycache__/"]

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.pyc\n\n\n__pycache__/\n")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == ["*.pyc", "__pycache__/"]

    def test_skips_comments(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("# this is a comment\n*.pyc\n# another comment\n__pycache__/\n")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == ["*.pyc", "__pycache__/"]

    def test_inline_comments_not_stripped(self, tmp_path: Path) -> None:
        """gitignore does not support inline comments."""
        gi = tmp_path / ".gitignore"
        gi.write_text("*.pyc # not a comment\n")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == ["*.pyc # not a comment"]


# -- 边界条件 --


class TestEdgeCases:
    def test_no_gitignore_returns_empty(self, tmp_path: Path) -> None:
        result = load_gitignore_patterns(str(tmp_path))
        assert result == []

    def test_empty_gitignore_returns_empty(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == []

    def test_only_comments_returns_empty(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("# comment1\n# comment2\n")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == []

    def test_only_blank_lines_returns_empty(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("\n\n\n")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == []

    def test_directory_path_without_gitignore(self, tmp_path: Path) -> None:
        result = load_gitignore_patterns(str(tmp_path / "nonexistent"))
        assert result == []


# -- 错误处理 --


class TestErrorHandling:
    def test_unreadable_file_returns_empty(self, tmp_path: Path) -> None:
        """If .gitignore is a directory (unreadable as file), should return empty."""
        gi = tmp_path / ".gitignore"
        gi.mkdir()
        # .gitignore is a directory, not a file
        result = load_gitignore_patterns(str(tmp_path))
        assert result == []

    def test_gitignore_with_special_characters(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.log\n!important.log\nbuild/\n")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == ["*.log", "!important.log", "build/"]

    def test_gitignore_with_glob_patterns(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("**/*.pyc\n*.egg-info/\n.env\n")
        result = load_gitignore_patterns(str(tmp_path))
        assert result == ["**/*.pyc", "*.egg-info/", ".env"]
