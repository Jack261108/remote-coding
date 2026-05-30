"""Property-based tests for the shared process-liveness probe.

Feature: external-binding-pid-liveness

Covers the two probe-level correctness properties from the design's
"Correctness Properties" section:

  - Property 1: Probe outcome mapping and no-signal guarantee (pid > 0)
  - Property 2: Non-positive pid short-circuits without calling os.kill

Target under test: ``app.services.process_liveness.process_is_alive``.

These probe tests are synchronous; ``os.kill`` is mocked so no real signal is
ever sent to any process.
"""

from __future__ import annotations

from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.process_liveness import process_is_alive

# Tagged os.kill outcomes for Property 1. Each tag maps to (configured mock
# behavior, expected process_is_alive result).
#   "returns"      -> os.kill returns normally           -> True
#   "no_process"   -> raises ProcessLookupError           -> False
#   "permission"   -> raises PermissionError              -> True
#   "other_oserror"-> raises a generic OSError            -> True
_OUTCOME_TAGS = ["returns", "no_process", "permission", "other_oserror"]


# --- Property 1: Probe outcome mapping and no-signal guarantee (pid > 0) -----


# Feature: external-binding-pid-liveness, Property 1: Probe outcome mapping and no-signal guarantee (pid > 0)
@settings(max_examples=200)
@given(
    pid=st.integers(min_value=1),
    outcome=st.sampled_from(_OUTCOME_TAGS),
)
def test_property_1_probe_outcome_mapping_and_no_signal(pid: int, outcome: str) -> None:
    """For any pid > 0 and any mocked os.kill outcome, process_is_alive maps:
    returns -> True, ProcessLookupError -> False, PermissionError -> True, any
    other OSError -> True; and every os.kill invocation uses signal 0 only.

    **Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.6**
    """
    if outcome == "returns":
        side_effect: object = None
        expected = True
    elif outcome == "no_process":
        side_effect = ProcessLookupError()
        expected = False
    elif outcome == "permission":
        side_effect = PermissionError()
        expected = True
    else:  # "other_oserror" - a generic OSError that is not one of the above
        side_effect = OSError("ambiguous probe error")
        expected = True

    with patch("app.services.process_liveness.os.kill") as mock_kill:
        if isinstance(side_effect, BaseException):
            mock_kill.side_effect = side_effect
        else:
            mock_kill.return_value = None

        result = process_is_alive(pid)

    # Outcome mapping (Requirements 1.2-1.5).
    assert result is expected, f"process_is_alive({pid}) with outcome={outcome!r} returned {result!r}, expected {expected!r}"

    # No-signal guarantee (Requirement 1.6): os.kill was called and EVERY call
    # used signal 0 (the existence probe), so no real signal is ever sent.
    assert mock_kill.call_args_list, "os.kill should be called for a positive pid"
    for call in mock_kill.call_args_list:
        assert call.args[0] == pid, f"os.kill called with pid {call.args[0]!r}, expected {pid!r}"
        assert call.args[1] == 0, f"os.kill must use signal 0, got {call.args[1]!r}"


# --- Property 2: Non-positive pid short-circuits without calling os.kill ------


# Feature: external-binding-pid-liveness, Property 2: Non-positive pid short-circuits without calling os.kill
@settings(max_examples=200)
@given(pid=st.integers(max_value=0))
def test_property_2_non_positive_pid_short_circuits(pid: int) -> None:
    """For any pid <= 0, process_is_alive returns False and never calls os.kill.

    **Validates: Requirements 1.7**
    """
    with patch("app.services.process_liveness.os.kill") as mock_kill:
        result = process_is_alive(pid)

    assert result is False, f"process_is_alive({pid}) should return False for non-positive pid"
    mock_kill.assert_not_called()
