"""Property-based tests for missing required configuration detection.

Feature: homebrew-packaging, Property 3: 必填配置缺失检测

**Validates: Requirements 1.6, 5.4, 5.5**
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from app.config.loader import REQUIRED_FIELDS, missing_required_fields


_required_aliases = list(REQUIRED_FIELDS.keys())

_value_st = st.one_of(
    st.text(min_size=1, max_size=50),
    st.just("   "),
    st.just(""),
)

_optional_env_st = st.dictionaries(
    keys=st.text(min_size=1, max_size=20, alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_"),
    values=st.text(min_size=0, max_size=50),
    max_size=5,
)


def _make_mapping(present: dict[str, str], missing_keys: set[str]) -> dict[str, str]:
    """Build a mapping that has values for present keys, omits missing_keys."""
    result = dict(present)
    for k in missing_keys:
        result.pop(k, None)
    return result


@settings(max_examples=100)
@given(
    env_present=st.dictionaries(
        keys=st.sampled_from(_required_aliases),
        values=_value_st.filter(lambda v: v.strip() != ""),
        min_size=0,
        max_size=len(_required_aliases),
    ),
    dotenv_present=st.dictionaries(
        keys=st.sampled_from(_required_aliases),
        values=_value_st.filter(lambda v: v.strip() != ""),
        min_size=0,
        max_size=len(_required_aliases),
    ),
)
def test_missing_fields_exactly_both_sources_blank(
    env_present: dict[str, str],
    dotenv_present: dict[str, str],
) -> None:
    """missing_required_fields returns items blank/missing in BOTH sources."""
    env = dict(env_present)
    dotenv = dict(dotenv_present)

    # Determine which aliases are blank/missing in both
    expected_missing = []
    for alias in _required_aliases:
        env_blank = alias not in env or str(env[alias]).strip() == ""
        dotenv_blank = alias not in dotenv or str(dotenv[alias]).strip() == ""
        if env_blank and dotenv_blank:
            expected_missing.append(alias)

    result = missing_required_fields(env, dotenv)
    assert result == expected_missing
