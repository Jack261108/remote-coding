"""Unit tests for CLI argument parsing.

**Validates: Requirements 1.2, 1.4, 1.7**
"""

from __future__ import annotations

import pytest

from app.main import CliOptions, build_arg_parser, parse_cli


class TestBuildArgParser:
    """build_arg_parser produces a parser with expected usage text."""

    def test_help_text_contains_prog_name(self) -> None:
        parser = build_arg_parser()
        assert "tg-cli-gateway" in parser.format_help()

    def test_help_text_contains_version_flag(self) -> None:
        parser = build_arg_parser()
        assert "--version" in parser.format_help()

    def test_help_text_contains_help_flag(self) -> None:
        parser = build_arg_parser()
        assert "--help" in parser.format_help()

    def test_help_text_contains_env_file_flag(self) -> None:
        parser = build_arg_parser()
        assert "--env-file" in parser.format_help()


class TestParseCli:
    """parse_cli handles known and unknown arguments correctly."""

    def test_empty_argv_returns_gateway_action(self) -> None:
        opts = parse_cli([])
        assert isinstance(opts, CliOptions)
        assert opts.env_file is None

    def test_env_file_stores_path(self) -> None:
        opts = parse_cli(["--env-file", "/tmp/test.env"])
        assert opts.env_file == "/tmp/test.env"

    def test_unknown_arg_exits_code_two(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            parse_cli(["--bogus"])
        assert exc_info.value.code == 2

    def test_version_exits_code_zero(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            parse_cli(["--version"])
        assert exc_info.value.code == 0

    def test_help_exits_code_zero(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            parse_cli(["--help"])
        assert exc_info.value.code == 0
