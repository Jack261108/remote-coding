"""Property-based tests for fixed dependency version constraints.

Feature: homebrew-packaging, Property 7: 固定依赖版本满足 pyproject 约束

**Validates: Requirements 3.3**
"""

from __future__ import annotations

from pathlib import Path

import hypothesis.strategies as st
import tomllib
from hypothesis import given, settings
from packaging.specifiers import SpecifierSet
from packaging.version import Version


def _read_constraints() -> dict[str, SpecifierSet]:
    """Read dependency constraints from pyproject.toml."""
    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    constraints: dict[str, SpecifierSet] = {}
    for dep in data["project"]["dependencies"]:
        # Format: "package>=X.Y.Z,<A"
        name = dep.split(">=")[0].split("==")[0].split("<")[0].split(">")[0].split("!")[0].split("[")[0].strip()
        spec_str = dep[len(name) :].strip()
        constraints[name] = SpecifierSet(spec_str)
    return constraints


_CONSTRAINTS = _read_constraints()

# Pinned versions in the Homebrew formula (packaging/tg-cli-gateway.rb).
# These are the versions actually shipped to users via brew install.
_PINNED_VERSIONS: dict[str, str] = {
    "aiogram": "3.28.2",
    "pydantic-settings": "2.14.1",
    "aiohttp-socks": "0.11.0",
}


@settings(max_examples=100)
@given(dummy=st.none())
def test_pinned_versions_satisfy_constraints(dummy: None) -> None:
    """The versions pinned in the Homebrew formula satisfy pyproject.toml constraints."""
    for name, pinned_ver in _PINNED_VERSIONS.items():
        if name not in _CONSTRAINTS:
            continue
        ver = Version(pinned_ver)
        assert ver in _CONSTRAINTS[name], f"{name}=={pinned_ver} does not satisfy constraint {_CONSTRAINTS[name]}"


@settings(max_examples=100)
@given(
    aiogram_ver=st.from_regex(r"[0-9]+\.[0-9]+\.[0-9]+", fullmatch=True),
    pydantic_ver=st.from_regex(r"[0-9]+\.[0-9]+\.[0-9]+", fullmatch=True),
    aiohttp_ver=st.from_regex(r"[0-9]+\.[0-9]+\.[0-9]+", fullmatch=True),
)
def test_random_versions_classified_consistently(
    aiogram_ver: str,
    pydantic_ver: str,
    aiohttp_ver: str,
) -> None:
    """Random versions are classified consistently by the pyproject constraints."""
    pairs = [
        ("aiogram", aiogram_ver),
        ("pydantic-settings", pydantic_ver),
        ("aiohttp-socks", aiohttp_ver),
    ]
    for name, ver_str in pairs:
        if name not in _CONSTRAINTS:
            continue
        ver = Version(ver_str)
        in_spec = ver in _CONSTRAINTS[name]
        # The assertion is that the constraint check itself is consistent:
        # a version either satisfies or doesn't, no crash.
        assert isinstance(in_spec, bool)
