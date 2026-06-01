"""Property-based tests for CLI argument dispatch.

Feature: homebrew-packaging, Property 1: 参数分发的退出码与启动不变式

**Validates: Requirements 1.3, 1.4, 1.5, 1.7**
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from app.main import CliOptions, parse_cli


# --- Strategies ---

_version_flag_st = st.sampled_from(["--version"])
_help_flag_st = st.sampled_from(["--help", "-h"])
_unknown_flag_st = (
    st.text(min_size=2, max_size=20, alphabet="abcdef-")
    .map(
        lambda s: f"--{s}" if not s.startswith("-") else s,
    )
    .filter(lambda s: s not in {"--", "--version", "--help", "--env-file", "-h"})
)
_env_file_path_st = st.text(min_size=1, max_size=100, alphabet="abcdefghijklmnopqrstuvwxyz0123456789/._").filter(
    lambda s: not s.startswith("-"),
)


@settings(max_examples=100)
@given(flag=_version_flag_st)
def test_version_flag_exits_zero(flag: str) -> None:
    """--version raises SystemExit(0)."""
    try:
        parse_cli([flag])
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 0


@settings(max_examples=100)
@given(flag=_help_flag_st)
def test_help_flag_exits_zero(flag: str) -> None:
    """--help/-h raises SystemExit(0)."""
    try:
        parse_cli([flag])
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 0


@settings(max_examples=100)
@given(flag=_unknown_flag_st)
def test_unknown_flag_exits_two(flag: str) -> None:
    """Unknown flags raise SystemExit(2)."""
    try:
        parse_cli([flag])
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 2


@settings(max_examples=100)
@given(path=_env_file_path_st)
def test_env_file_option_parsed(path: str) -> None:
    """--env-file <path> stores the path in CliOptions.env_file."""
    opts = parse_cli(["--env-file", path])
    assert isinstance(opts, CliOptions)
    assert opts.env_file == path


@settings(max_examples=1)
@given(dummy=st.none())
def test_empty_argv_starts_gateway(dummy: None) -> None:
    """parse_cli([]) produces CliOptions with env_file=None."""
    opts = parse_cli([])
    assert isinstance(opts, CliOptions)
    assert opts.env_file is None
