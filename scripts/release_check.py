"""Release-side pure-logic helpers for the Homebrew packaging pipeline.

This module hosts the version gate and checksum utilities consumed by the
release workflow (``.github/workflows/release.yml``). The functions are kept
pure and importable so they can be covered by unit and property-based tests.

Single version source of truth: ``pyproject.toml`` ``[project].version``.
"""

from __future__ import annotations

import hashlib
import re
import tomllib

# Number of bytes read per chunk when hashing files, keeping memory usage
# bounded regardless of file size.
_CHUNK_SIZE = 65536


def normalize_tag(tag: str) -> str:
    """Normalize a Git tag to a bare semantic version string.

    Strips surrounding whitespace and a single leading ``v`` prefix.

    Examples:
        ``"v0.1.3"`` -> ``"0.1.3"``
        ``"0.1.3"``  -> ``"0.1.3"``
        ``"  v0.1.3 "`` -> ``"0.1.3"``
    """
    stripped = tag.strip()
    if stripped.startswith("v"):
        return stripped[1:]
    return stripped


def read_pyproject_version(path: str = "pyproject.toml") -> str:
    """Read ``[project].version`` from a ``pyproject.toml`` file.

    Uses the standard library ``tomllib`` (Python 3.11+) and reads the file in
    binary mode as required by ``tomllib.load``.
    """
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]


def versions_match(tag: str, pyproject_version: str) -> bool:
    """Return ``True`` when the tag (minus leading ``v``) equals the version.

    This is the core of the release version gate: the tag that triggered the
    pipeline must agree with ``pyproject.toml``'s ``[project].version``.
    """
    return normalize_tag(tag) == pyproject_version


def sha256_of(path: str) -> str:
    """Compute the SHA-256 hex digest of a file.

    Reads the file in binary chunks to bound memory usage, returning the
    lowercase hexadecimal digest string.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- Homebrew formula update / parse-back helpers --------------------------
#
# The formula's release download URL has a fixed, known shape for this
# repository, e.g.::
#
#     url "https://github.com/Jack261108/remote-coding/releases/download/v0.1.2/tg-cli-gateway-0.1.2.tar.gz"
#     sha256 "<hex>"
#
# The version appears twice in the URL: once as the ``vX.Y.Z`` release-tag
# segment and once in the ``tg-cli-gateway-X.Y.Z.tar.gz`` filename. The main
# package ``sha256`` line sits immediately after this ``url`` line.
#
# Anchoring to this exact shape lets ``update_formula`` rewrite the version and
# checksum deterministically, and lets the parse-back helpers read them for
# round-trip assertions (Property 8). Pinning the ``sha256`` to the line that
# directly follows the release URL avoids touching the per-``resource``
# ``sha256`` values elsewhere in the formula.

_RELEASE_DOWNLOAD_PREFIX = "https://github.com/Jack261108/remote-coding/releases/download"

# Matches the main formula ``url`` line together with the ``sha256`` line that
# immediately follows it (tolerant of surrounding whitespace / indentation).
_MAIN_URL_SHA_RE = re.compile(
    r'(?P<url_prefix>url\s+")'
    r"(?P<url>" + re.escape(_RELEASE_DOWNLOAD_PREFIX) + r"/"
    r'v(?P<tag_ver>[^/"]+?)/tg-cli-gateway-(?P<file_ver>[^/"]+?)\.tar\.gz)'
    r'(?P<url_suffix>")'
    r'(?P<between>\s*sha256\s+")'
    r"(?P<digest>[0-9a-fA-F]+)"
    r'(?P<sha_suffix>")'
)

# Optional explicit ``version "X.Y.Z"`` declaration. Homebrew normally infers
# the version from the URL, so the design formula omits this line; we still
# update it when present so the version is consistent everywhere it is written.
_EXPLICIT_VERSION_RE = re.compile(
    r'(?P<prefix>^[^\S\n]*version\s+")(?P<ver>[^"]*)(?P<suffix>")',
    re.MULTILINE,
)


def _require_main_match(text: str) -> re.Match[str]:
    """Locate the main release ``url`` + ``sha256`` pair in formula ``text``.

    Raises ``ValueError`` when the formula does not contain a recognizable
    GitHub release download URL immediately followed by a ``sha256`` line.
    """
    match = _MAIN_URL_SHA_RE.search(text)
    if match is None:
        raise ValueError("formula text does not contain a recognizable release download url followed by a sha256 line")
    return match


def update_formula(text: str, version: str, sha256: str) -> str:
    """Rewrite the formula's version and checksum to ``version`` / ``sha256``.

    Updates, within the main release download URL, both the ``vX.Y.Z`` tag
    segment and the ``tg-cli-gateway-X.Y.Z.tar.gz`` filename segment, replaces
    the ``sha256`` digest on the line directly following that URL, and updates
    an explicit ``version "..."`` declaration if one is present. Returns the
    updated formula text.

    Raises ``ValueError`` when the expected release URL / ``sha256`` shape is
    not found, so a malformed formula fails loudly rather than silently.
    """

    def _replace_main(match: re.Match[str]) -> str:
        return (
            f"{match.group('url_prefix')}"
            f"{_RELEASE_DOWNLOAD_PREFIX}/"
            f"v{version}/tg-cli-gateway-{version}.tar.gz"
            f"{match.group('url_suffix')}"
            f"{match.group('between')}"
            f"{sha256}"
            f"{match.group('sha_suffix')}"
        )

    updated, count = _MAIN_URL_SHA_RE.subn(_replace_main, text, count=1)
    if count == 0:
        raise ValueError("formula text does not contain a recognizable release download url followed by a sha256 line")

    updated = _EXPLICIT_VERSION_RE.sub(
        lambda match: f"{match.group('prefix')}{version}{match.group('suffix')}",
        updated,
    )
    return updated


def parse_formula_url(text: str) -> str:
    """Return the main release download ``url`` value from formula ``text``."""
    return _require_main_match(text).group("url")


def parse_formula_version(text: str) -> str:
    """Return the version reflected in the formula's release download URL.

    Both URL version segments (the ``vX.Y.Z`` tag segment and the
    ``tg-cli-gateway-X.Y.Z.tar.gz`` filename segment) must agree; otherwise a
    ``ValueError`` is raised since the formula version would be ambiguous.
    """
    match = _require_main_match(text)
    tag_version = match.group("tag_ver")
    file_version = match.group("file_ver")
    if tag_version != file_version:
        raise ValueError(f"formula url version segments disagree: tag={tag_version!r} filename={file_version!r}")
    return tag_version


def parse_formula_sha256(text: str) -> str:
    """Return the main package ``sha256`` digest from formula ``text``."""
    return _require_main_match(text).group("digest")
