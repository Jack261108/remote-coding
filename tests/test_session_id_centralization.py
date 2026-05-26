# Feature: deduplicate-session-id-utils, Task 3.1
"""Verify centralized session ID utilities are properly exported and shared."""

from __future__ import annotations

from app.domain.session_models import CLAUDE_SESSION_PREFIX, _UUID_SESSION_RE, is_claude_session_id


def test_claude_session_prefix_value() -> None:
    """CLAUDE_SESSION_PREFIX has the canonical value."""
    assert CLAUDE_SESSION_PREFIX == "claude-session-"


def test_uuid_session_re_matches_valid_uuids() -> None:
    """_UUID_SESSION_RE matches known valid UUIDs."""
    valid_uuids = [
        "550e8400-e29b-41d4-a716-446655440000",  # v4
        "6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # v1
        "3d813cbb-47fb-32ba-91df-831e1593ac29",  # v3
        "21f7f8de-8051-5b89-8680-0195ef798b6a",  # v5
        "F47AC10B-58CC-4372-A567-0E02B2C3D479",  # uppercase
    ]
    for uid in valid_uuids:
        assert _UUID_SESSION_RE.match(uid), f"Should match valid UUID: {uid}"


def test_uuid_session_re_rejects_invalid_strings() -> None:
    """_UUID_SESSION_RE rejects non-UUID strings."""
    invalid = [
        "",
        "not-a-uuid",
        "550e8400-e29b-41d4-a716",  # truncated
        "550e8400-e29b-61d4-a716-446655440000",  # v6 (not supported)
        "550e8400-e29b-01d4-a716-446655440000",  # v0 (not valid)
        "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx",  # template
    ]
    for s in invalid:
        assert not _UUID_SESSION_RE.match(s), f"Should reject: {s}"


def test_session_store_uses_canonical_is_claude_session_id() -> None:
    """session_store.py imports is_claude_session_id from the canonical location."""
    from app.services import session_store

    # The module-level import should be the same object
    store_fn = getattr(session_store, "is_claude_session_id", None)
    assert store_fn is is_claude_session_id, "session_store.is_claude_session_id should be imported from app.domain.session_models"


def test_session_state_cache_uses_canonical_is_claude_session_id() -> None:
    """session_state_cache.py imports is_claude_session_id from the canonical location."""
    from app.services import session_state_cache

    cache_fn = getattr(session_state_cache, "is_claude_session_id", None)
    assert cache_fn is is_claude_session_id, "session_state_cache.is_claude_session_id should be imported from app.domain.session_models"
