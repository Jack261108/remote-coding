"""Property-based tests for Env_File path classification.

Feature: homebrew-packaging, Property 5: Env_File 路径分类（显式不可读 vs 默认缺失）

**Validates: Requirements 5.6, 5.7**
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from app.config.loader import EnvFileAction, classify_env_file

_env_file_st = st.one_of(st.none(), st.text(min_size=1, max_size=100))
_is_readable_st = st.booleans()
_default_env_exists_st = st.booleans()


@settings(max_examples=100)
@given(
    env_file=_env_file_st,
    is_readable=_is_readable_st,
    default_env_exists=_default_env_exists_st,
)
def test_classify_env_file_properties(
    env_file: str | None,
    is_readable: bool,
    default_env_exists: bool,
) -> None:
    """classify_env_file satisfies the decision table."""
    result = classify_env_file(env_file, is_readable, default_env_exists=default_env_exists)

    if env_file is not None and not is_readable:
        assert result is EnvFileAction.ERROR_UNREADABLE
    elif env_file is None and not default_env_exists:
        assert result is EnvFileAction.FALLBACK
    else:
        assert result is EnvFileAction.LOAD


@settings(max_examples=100)
@given(
    env_file=st.text(min_size=1, max_size=100),
    is_readable=st.just(False),
)
def test_explicit_unreadable_never_fallback(env_file: str, is_readable: bool) -> None:
    """Explicit unreadable path never yields FALLBACK."""
    result = classify_env_file(env_file, is_readable, default_env_exists=False)
    assert result is EnvFileAction.ERROR_UNREADABLE
    assert result is not EnvFileAction.FALLBACK


@settings(max_examples=100)
@given(default_env_exists=st.booleans())
def test_none_no_default_always_fallback(default_env_exists: bool) -> None:
    """When env_file=None: FALLBACK if default missing, LOAD if default exists."""
    result = classify_env_file(None, False, default_env_exists=default_env_exists)
    if default_env_exists:
        assert result is EnvFileAction.LOAD
    else:
        assert result is EnvFileAction.FALLBACK
