"""Property-based tests for tmux preflight check.

Feature: homebrew-packaging, Property 6: tmux 预检查

**Validates: Requirements 4.3, 4.4, 4.5**
"""

from __future__ import annotations

from collections.abc import Callable

import hypothesis.strategies as st
from hypothesis import given, settings

from app.infra.tmux_preflight import tmux_preflight

_resolver_st = st.sampled_from(
    [
        lambda _bin: "/usr/bin/tmux",  # found
        lambda _bin: None,  # not found
    ]
)


@settings(max_examples=100)
@given(
    tmux_mode=st.booleans(),
    resolver=_resolver_st,
)
def test_preflight_result_properties(
    tmux_mode: bool,
    resolver: Callable[[str], str | None],
) -> None:
    """tmux_preflight returns ok=False iff tmux_mode=True and resolver fails."""
    result = tmux_preflight(tmux_mode, "tmux", resolver=resolver)

    if not tmux_mode:
        assert result.ok is True
        assert result.error is None
    elif resolver("tmux") is not None:
        assert result.ok is True
        assert result.error is None
    else:
        assert result.ok is False
        assert result.error is not None
        assert "tmux" in result.error.lower()


@settings(max_examples=100)
@given(tmux_mode=st.just(False))
def test_mode_false_always_ok(tmux_mode: bool) -> None:
    """tmux_mode=False always returns ok regardless of resolver."""
    result = tmux_preflight(tmux_mode, "tmux", resolver=lambda _: None)
    assert result.ok is True
    assert result.error is None


@settings(max_examples=100)
@given(tmux_mode=st.just(True))
def test_mode_true_found_ok(tmux_mode: bool) -> None:
    """tmux_mode=True with resolver finding tmux returns ok."""
    result = tmux_preflight(tmux_mode, "tmux", resolver=lambda _: "/usr/bin/tmux")
    assert result.ok is True
    assert result.error is None
