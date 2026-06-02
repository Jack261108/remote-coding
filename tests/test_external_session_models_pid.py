"""Unit tests for the `pid` field on the `ExternalBinding` dataclass.

Spec: external-binding-pid-liveness, Task 3.2

Covers Requirement 2.1: `ExternalBinding` includes a field `pid` of type
`int | None`. These tests confirm the field's default value and that the
dataclass remains constructible both with and without `pid` while the
`last_activity_at` InitVar default behavior (falling back to `bound_at`)
continues to hold in both cases.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.external_session_models import ExternalBinding

# A fixed, tz-aware bind time so assertions are deterministic.
_BOUND_AT = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_pid_defaults_to_none_when_not_provided() -> None:
    """Default `pid` is `None` when constructed without it.

    Validates: Requirements 2.1
    """
    binding = ExternalBinding(
        session_id="sess-1",
        user_id=42,
        cwd="/tmp/project",
        bound_at=_BOUND_AT,
        jsonl_path=None,
    )

    assert binding.pid is None
    # InitVar default: last_activity_at falls back to bound_at.
    assert binding.last_activity_at == _BOUND_AT


def test_constructible_with_pid_and_activity_defaults() -> None:
    """The dataclass is constructible WITH `pid` and still defaults activity.

    Validates: Requirements 2.1
    """
    binding = ExternalBinding(
        session_id="sess-2",
        user_id=7,
        cwd="/tmp/work",
        bound_at=_BOUND_AT,
        jsonl_path="/tmp/work/session.jsonl",
        pid=4242,
    )

    assert binding.pid == 4242
    # last_activity_at still defaults to bound_at via __post_init__ even when
    # pid is supplied.
    assert binding.last_activity_at == _BOUND_AT


def test_constructible_without_pid_and_activity_defaults() -> None:
    """The dataclass is constructible WITHOUT `pid` and defaults activity.

    Validates: Requirements 2.1
    """
    binding = ExternalBinding(
        session_id="sess-3",
        user_id=99,
        cwd="/tmp/other",
        bound_at=_BOUND_AT,
        jsonl_path=None,
    )

    assert binding.pid is None
    assert binding.last_activity_at == _BOUND_AT
