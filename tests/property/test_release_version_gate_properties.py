"""Property-based tests for release version gate (versions_match).

Feature: homebrew-packaging, Property 2: 版本一致性不变式（versions_match 部分）

**Validates: Requirements 2.5, 7.4, 7.5**
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from scripts.release_check import normalize_tag, versions_match


_semver_st = st.from_regex(r"[0-9]+\.[0-9]+\.[0-9]+", fullmatch=True)
_tag_st = st.one_of(
    _semver_st.map(lambda v: f"v{v}"),
    _semver_st,
)


@settings(max_examples=100)
@given(tag=_tag_st, version=_semver_st)
def test_versions_match_iff_normalized_eq(tag: str, version: str) -> None:
    """versions_match(tag, v) is True iff normalize_tag(tag) == v."""
    expected = normalize_tag(tag) == version
    assert versions_match(tag, version) == expected


@settings(max_examples=100)
@given(version=_semver_st)
def test_v_prefix_matches_bare(version: str) -> None:
    """vX.Y.Z tag matches bare X.Y.Z version."""
    assert versions_match(f"v{version}", version) is True


@settings(max_examples=100)
@given(version=_semver_st)
def test_bare_matches_bare(version: str) -> None:
    """X.Y.Z tag matches X.Y.Z version."""
    assert versions_match(version, version) is True
