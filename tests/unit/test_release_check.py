"""Unit tests for release-side pure-logic helpers.

**Validates: Requirements 7.1, 7.4**
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from scripts.release_check import (
    normalize_tag,
    parse_formula_sha256,
    parse_formula_url,
    parse_formula_version,
    read_pyproject_version,
    sha256_of,
    update_formula,
    versions_match,
)


class TestNormalizeTag:
    def test_v_prefix_stripped(self) -> None:
        assert normalize_tag("v0.1.3") == "0.1.3"

    def test_bare_version_unchanged(self) -> None:
        assert normalize_tag("0.1.3") == "0.1.3"

    def test_whitespace_stripped(self) -> None:
        assert normalize_tag("  v0.1.3  ") == "0.1.3"


class TestReadPyprojectVersion:
    def test_returns_current_version(self) -> None:
        version = read_pyproject_version()
        assert version == "0.1.2"

    def test_custom_path(self) -> None:
        version = read_pyproject_version("pyproject.toml")
        assert version == "0.1.2"


class TestVersionsMatch:
    def test_v_prefix_matches(self) -> None:
        assert versions_match("v0.1.3", "0.1.3") is True

    def test_bare_matches(self) -> None:
        assert versions_match("0.1.3", "0.1.3") is True

    def test_mismatch(self) -> None:
        assert versions_match("v0.1.3", "0.2.0") is False


class TestSha256Of:
    def test_known_content(self) -> None:
        content = b"hello world"
        expected = hashlib.sha256(content).hexdigest()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            path = f.name
        try:
            assert sha256_of(path) == expected
        finally:
            Path(path).unlink(missing_ok=True)


class TestUpdateFormula:
    _FORMULA = """\
class TgCliGateway < Formula
  url "https://github.com/Jack261108/remote-coding/releases/download/v0.1.2/tg-cli-gateway-0.1.2.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
end
"""

    def test_updates_version_and_sha(self) -> None:
        result = update_formula(self._FORMULA, "1.2.3", "a" * 64)
        assert parse_formula_version(result) == "1.2.3"
        assert parse_formula_sha256(result) == "a" * 64

    def test_url_updated(self) -> None:
        result = update_formula(self._FORMULA, "1.2.3", "a" * 64)
        url = parse_formula_url(result)
        assert "v1.2.3" in url
        assert "tg-cli-gateway-1.2.3.tar.gz" in url

    def test_malformed_formula_raises(self) -> None:
        with pytest.raises(ValueError, match="does not contain"):
            update_formula("no url here", "1.0.0", "a" * 64)


class TestParseFormulaHelpers:
    _FORMULA = """\
class TgCliGateway < Formula
  url "https://github.com/Jack261108/remote-coding/releases/download/v0.1.2/tg-cli-gateway-0.1.2.tar.gz"
  sha256 "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
end
"""

    def test_parse_url(self) -> None:
        url = parse_formula_url(self._FORMULA)
        assert "v0.1.2" in url
        assert "tg-cli-gateway-0.1.2.tar.gz" in url

    def test_parse_version(self) -> None:
        assert parse_formula_version(self._FORMULA) == "0.1.2"

    def test_parse_sha256(self) -> None:
        assert parse_formula_sha256(self._FORMULA) == "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

    def test_version_segments_mismatch_raises(self) -> None:
        bad = self._FORMULA.replace("tg-cli-gateway-0.1.2", "tg-cli-gateway-9.9.9")
        with pytest.raises(ValueError, match="disagree"):
            parse_formula_version(bad)
