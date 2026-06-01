"""Property-based tests for version consistency (app-side get_version).

Feature: homebrew-packaging, Property 2: 版本一致性不变式（get_version 部分）

**Validates: Requirements 1.3, 2.5**
"""

from __future__ import annotations

from pathlib import Path

import hypothesis.strategies as st
from hypothesis import given, settings

from app.main import get_version


def _read_pyproject_version_from_root() -> str:
    """Read [project].version from the repository root pyproject.toml."""
    import tomllib

    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]


@settings(max_examples=100)
@given(dummy=st.none())
def test_get_version_matches_pyproject(dummy: None) -> None:
    """get_version() returns the same version as pyproject.toml."""
    expected = _read_pyproject_version_from_root()
    assert get_version() == expected
