"""Property-based test for the cleanup Decision Matrix.

Feature: external-binding-pid-liveness

Covers the Decision-Matrix correctness property from the design's "Correctness
Properties" section:

  - Property 3: Decision Matrix is realized exactly by ``decide_cleanup``

Target under test: ``app.services.external_binding_cleanup_service.decide_cleanup``,
a pure, synchronous function returning a frozen ``CleanupDecision`` dataclass
with ``.action`` ("keep"|"remove") and ``.reason`` (str|None).

The expected decision is computed from an INDEPENDENT reference implementation
of the normative Decision Matrix (re-derived inline from requirements.md), so
the test does not merely echo ``decide_cleanup``'s own logic.

This test is synchronous (``decide_cleanup`` is pure/sync) and is therefore not
marked async. The input space is only 2^5 = 32 combinations, so 200 examples
exhaustively cover it.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.external_binding_cleanup_service import decide_cleanup


def expected(
    liveness_enabled: bool,
    pid_known: bool,
    pid_alive: bool,
    idle_expired: bool,
    has_pending: bool,
) -> tuple[str, str | None]:
    """Independent reference realization of the normative Decision Matrix.

    Re-derived directly from requirements.md (rows 1-9), NOT from
    ``decide_cleanup``'s implementation:

      - liveness enabled AND pid known: live pid -> KEEP (row 1); dead pid ->
        REMOVE/``pid_dead`` (rows 2, 3) regardless of idle age or pending.
      - otherwise (pid unknown OR liveness disabled): the legacy idle rule —
        REMOVE/``idle_ttl_expired`` iff idle expired and not pending (rows 4, 7),
        else KEEP (rows 5, 6, 8, 9).
    """
    if liveness_enabled and pid_known:
        if pid_alive:
            return ("keep", None)  # row 1
        return ("remove", "pid_dead")  # rows 2, 3
    if idle_expired and not has_pending:
        return ("remove", "idle_ttl_expired")  # rows 4, 7
    return ("keep", None)  # rows 5, 6, 8, 9


# Feature: external-binding-pid-liveness, Property 3: Decision Matrix is realized exactly by decide_cleanup
@settings(max_examples=200)
@given(
    liveness_enabled=st.booleans(),
    pid_known=st.booleans(),
    pid_alive=st.booleans(),
    idle_expired=st.booleans(),
    has_pending_permission=st.booleans(),
)
def test_property_3_decision_matrix_realized_exactly(
    liveness_enabled: bool,
    pid_known: bool,
    pid_alive: bool,
    idle_expired: bool,
    has_pending_permission: bool,
) -> None:
    """For any combination of the five matrix inputs, ``decide_cleanup`` returns
    the (action, reason) mandated by the normative Decision Matrix: rows 1-3 when
    liveness is enabled and the pid is known (a live pid keeps, a dead pid removes
    with ``pid_dead`` regardless of idle age or pending), and rows 4-9 otherwise
    (the legacy idle rule — ``idle_ttl_expired`` iff idle expired and not pending,
    else keep).

    Because the pid-unknown / liveness-disabled branch is asserted to equal the
    legacy idle rule verbatim, this single property also establishes the
    no-regression equivalence of Requirement 11.1 (the pid-unknown branch is the
    pre-feature idle decision).

    **Validates: Requirements 5.1, 6.1, 6.2, 6.3, 7.1, 7.2, 7.3, 7.4, 10.2, 11.1**
    """
    decision = decide_cleanup(
        liveness_enabled=liveness_enabled,
        pid_known=pid_known,
        pid_alive=pid_alive,
        idle_expired=idle_expired,
        has_pending_permission=has_pending_permission,
    )

    expected_decision = expected(
        liveness_enabled,
        pid_known,
        pid_alive,
        idle_expired,
        has_pending_permission,
    )

    assert (decision.action, decision.reason) == expected_decision, (
        "decide_cleanup("
        f"liveness_enabled={liveness_enabled}, pid_known={pid_known}, "
        f"pid_alive={pid_alive}, idle_expired={idle_expired}, "
        f"has_pending_permission={has_pending_permission}) returned "
        f"{(decision.action, decision.reason)!r}, expected {expected_decision!r}"
    )
