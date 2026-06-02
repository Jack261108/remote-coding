"""Property-based tests for formula update roundtrip consistency.

Feature: homebrew-packaging, Property 8: 公式更新往返一致

**Validates: Requirements 7.2**
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from scripts.release_check import (
    parse_formula_sha256,
    parse_formula_url,
    parse_formula_version,
    update_formula,
)

# A minimal formula template with the expected URL + sha256 shape.
_FORMULA_TEMPLATE = """\
class TgCliGateway < Formula
  desc "Telegram interactive remote CLI execution gateway"
  homepage "https://github.com/Jack261108/remote-coding"
  url "https://github.com/Jack261108/remote-coding/releases/download/v0.1.2/tg-cli-gateway-0.1.2.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  depends_on "python@3.11"
end
"""

_version_st = st.from_regex(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", fullmatch=True)
_sha256_st = st.text(min_size=64, max_size=64, alphabet="0123456789abcdef")


@settings(max_examples=100)
@given(version=_version_st, sha256=_sha256_st)
def test_roundtrip_version_sha256(version: str, sha256: str) -> None:
    """After update_formula, parse back yields the same version and sha256."""
    updated = update_formula(_FORMULA_TEMPLATE, version, sha256)
    assert parse_formula_version(updated) == version
    assert parse_formula_sha256(updated) == sha256


@settings(max_examples=100)
@given(version=_version_st, sha256=_sha256_st)
def test_url_contains_version(version: str, sha256: str) -> None:
    """The updated formula URL contains the new version in both segments."""
    updated = update_formula(_FORMULA_TEMPLATE, version, sha256)
    url = parse_formula_url(updated)
    assert f"v{version}" in url
    assert f"tg-cli-gateway-{version}.tar.gz" in url
