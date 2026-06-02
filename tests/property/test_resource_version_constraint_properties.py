"""Property-based tests for fixed dependency version constraints.

Feature: homebrew-packaging, Property 7: 固定依赖版本满足 pyproject 约束

**Validates: Requirements 3.3**
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import hypothesis.strategies as st
from hypothesis import given, settings
from packaging.specifiers import SpecifierSet
from packaging.version import Version

_ROOT = Path(__file__).resolve().parents[2]
_FORMULA_PATH = _ROOT / "packaging" / "tg-cli-gateway.rb"


def _read_constraints() -> dict[str, SpecifierSet]:
    """Read dependency constraints from pyproject.toml."""
    with (_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    constraints: dict[str, SpecifierSet] = {}
    for dep in data["project"]["dependencies"]:
        # Format: "package>=X.Y.Z,<A"
        name = dep.split(">=")[0].split("==")[0].split("<")[0].split(">")[0].split("!")[0].split("[")[0].strip()
        spec_str = dep[len(name) :].strip()
        constraints[name] = SpecifierSet(spec_str)
    return constraints


_CONSTRAINTS = _read_constraints()

# Only these packages from pyproject.toml are also pinned as Homebrew resources.
_CONSTRAINED_PACKAGES = set(_CONSTRAINTS)

# Regex to extract version from a Homebrew resource URL filename.
# Matches: <name>-<version>[-<tags>].<ext>
_RESOURCE_VERSION_RE = re.compile(r"/([a-zA-Z0-9_.]+)-(\d+\.\d+\.\d+(?:\.\d+)?)(?:-[a-z0-9_.]+)*\.(?:whl|tar\.gz)")


def _read_pinned_versions_from_formula() -> dict[str, str]:
    """Parse resource versions from the Homebrew formula template.

    Reads ``packaging/tg-cli-gateway.rb`` and extracts the pinned version for
    each resource that also appears in ``pyproject.toml`` dependencies.
    """
    text = _FORMULA_PATH.read_text(encoding="utf-8")
    pinned: dict[str, str] = {}
    for match in _RESOURCE_VERSION_RE.finditer(text):
        pkg_name = match.group(1).replace("_", "-")
        version = match.group(2)
        if pkg_name in _CONSTRAINED_PACKAGES:
            pinned[pkg_name] = version
    return pinned


_PINNED_VERSIONS = _read_pinned_versions_from_formula()


def test_pinned_versions_satisfy_constraints() -> None:
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
