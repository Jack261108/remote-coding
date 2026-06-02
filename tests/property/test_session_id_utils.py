# Feature: deduplicate-session-id-utils, Property tests for is_claude_session_id
"""Property-based tests for the centralized is_claude_session_id function."""

from __future__ import annotations

import re
import uuid

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.domain.session_models import _UUID_SESSION_RE, is_claude_session_id


# Reference implementation for behavioral equivalence
def _reference_is_claude_session_id(session_id: str | None) -> bool:
    if not session_id:
        return False
    text = str(session_id).strip()
    if not text:
        return False
    return text.startswith("claude-session-") or bool(
        re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            text,
            re.IGNORECASE,
        )
    )


# Strategy: valid claude session IDs
_valid_uuid_strategy = st.builds(lambda: str(uuid.uuid4()))
_valid_prefixed_strategy = st.builds(lambda u: f"claude-session-{u}", _valid_uuid_strategy)
_valid_session_id_strategy = st.one_of(_valid_uuid_strategy, _valid_prefixed_strategy)


@given(text=st.text())
@settings(max_examples=200)
def test_property_behavioral_equivalence_arbitrary_text(text: str) -> None:
    """Property 1: For any string, is_claude_session_id matches reference implementation."""
    assert is_claude_session_id(text) == _reference_is_claude_session_id(text)


@given(session_id=_valid_session_id_strategy)
@settings(max_examples=100)
def test_property_valid_ids_accepted(session_id: str) -> None:
    """Valid claude session IDs (UUID or prefixed UUID) are always accepted."""
    assert is_claude_session_id(session_id) is True


@given(text=st.text().filter(lambda t: not t.strip().startswith("claude-session-") and not _UUID_SESSION_RE.match(t.strip())))
@settings(max_examples=200)
def test_property_invalid_input_rejection(text: str) -> None:
    """Property 2: Strings without the prefix and not matching UUID pattern are rejected."""
    assume(text.strip())  # non-empty after strip
    assert is_claude_session_id(text) is False


def test_none_rejected() -> None:
    """None input always returns False."""
    assert is_claude_session_id(None) is False


def test_empty_string_rejected() -> None:
    """Empty string always returns False."""
    assert is_claude_session_id("") is False
    assert is_claude_session_id("   ") is False
