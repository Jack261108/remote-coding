"""Bug-condition exploration tests for ``StructuredReplyPresenter.prime()``.

Spec: ``.kiro/specs/presenter-duplicate-stale-reply``.

These tests pin **Property 1** from the design (the bug condition):

    Bug Condition (from design ``isBugCondition``):
        baseline_current_snapshot == True
        AND snapshot.turn_id IS None
        AND persisted_turn_id IS NOT None

    Expected Behavior (from design ``expectedBehavior``):
        After ``prime(baseline_current_snapshot=True)``,
        ``presenter._last_structured_turn_id == persisted_turn_id``.

The test is expected to **fail on the unfixed code**: the current
``prime()`` (``app/bot/presenters/structured_reply_presenter.py:117-119``)
unconditionally overwrites ``_last_structured_turn_id`` with
``snapshot.turn_id`` (``None`` here), which discards the persisted reply
cursor that points at the previously acknowledged turn ``A``. On the next
pump, the loader returns ``A`` again and ``_collect_reply`` re-emits it as
a brand-new ``StructuredReplyOutput`` -- the duplicate "stale reply" the
user sees in chat mode.

Counterexample observed on UNFIXED code (with ``persisted_turn_id="turn-A"``):

    After ``await presenter.prime(baseline_current_snapshot=True)``:
        presenter._last_structured_turn_id == None    # BUG: expected "turn-A"

    First ``await presenter.poll(task_id="task-1")`` (snapshot still shows
    ``turn-A`` as the latest completed assistant turn):
        [StructuredReplyOutput(text="stale reply", turn_id="turn-A")]
        # BUG: expected []

Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3
"""

from __future__ import annotations

import asyncio

import hypothesis
import hypothesis.strategies as st
import pytest  # noqa: F401  # imported so pytest-asyncio plugin is loaded for sibling files

from app.bot.presenters.structured_reply_presenter import (
    StructuredReplyOutput,
    StructuredReplyPresenter,
)
from app.domain.session_models import ConversationTurn, SessionPhase
from tests.fakes.structured import make_structured_session as _session

# A turn id that can never collide with the hypothesis-generated
# ``persisted_turn_id`` (which always starts with the literal prefix ``turn-``).
_FRESH_TURN_ID = "FRESH-TURN-B"


class _DummyTaskServiceWithPersistedCursor:
    """Fake task service that mirrors ``DummyTaskService`` from
    ``tests/test_structured_reply_presenter.py`` but lets the test pin the
    persisted reply cursor returned by ``get_structured_reply_cursor``."""

    def __init__(self, sessions: list[object], *, persisted_turn_id: str) -> None:
        self._sessions = sessions
        self._index = 0
        self._persisted_turn_id = persisted_turn_id

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        if self._index >= len(self._sessions):
            return self._sessions[-1]
        session = self._sessions[self._index]
        self._index += 1
        return session

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return self._index

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        # Models the persisted cursor: previously acknowledged turn id, no permission key.
        return self._persisted_turn_id, None

    async def acknowledge_structured_reply(
        self,
        user_id: int,
        *,
        turn_id: str | None = None,
        permission_key: str | None = None,
        task_id: str | None = None,
    ) -> None:
        return None

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None):
        return None

    async def acknowledge_structured_user_question(
        self,
        user_id: int,
        *,
        question_key: str | None = None,
        task_id: str | None = None,
    ) -> None:
        return None

    async def wait_for_structured_session_update(
        self,
        *,
        user_id: int,
        since_cursor: int,
        timeout_sec: float,
        task_id: str | None = None,
    ) -> bool:
        return True


async def _run_bug_condition_scenario(persisted_turn_id: str):
    """Exercises the bug-condition scenario described in the design.

    Returns ``(cursor_after_prime, first_poll, second_poll)``.
    """
    sessions = [
        # 1) Prime-time snapshot: no turns yet -> snapshot.turn_id is None.
        _session(phase=SessionPhase.WAITING_FOR_INPUT),
        # 2) Next-poll snapshot: the previously acknowledged turn is still the
        #    latest completed assistant turn surfaced by the JSONL loader.
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[
                ConversationTurn(
                    turn_id=persisted_turn_id,
                    role="assistant",
                    text="\nstale reply\n",
                    is_complete=True,
                ),
            ],
        ),
        # 3) Subsequent snapshot: a fresh assistant turn has now completed.
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[
                ConversationTurn(
                    turn_id=persisted_turn_id,
                    role="assistant",
                    text="\nstale reply\n",
                    is_complete=True,
                ),
                ConversationTurn(
                    turn_id=_FRESH_TURN_ID,
                    role="assistant",
                    text="\nfresh reply\n",
                    is_complete=True,
                ),
            ],
        ),
    ]
    service = _DummyTaskServiceWithPersistedCursor(sessions, persisted_turn_id=persisted_turn_id)
    presenter = StructuredReplyPresenter(task_service=service, user_id=1)

    await presenter.prime(baseline_current_snapshot=True)
    cursor_after_prime = presenter._last_structured_turn_id

    first_poll = await presenter.poll(task_id="task-1")
    second_poll = await presenter.poll(task_id="task-1")
    return cursor_after_prime, first_poll, second_poll


# Restrict to printable ASCII (no whitespace, no control chars) so generated
# turn ids round-trip cleanly through dataclasses, logging extras, and
# normalize_stream_text. The ``turn-`` prefix guarantees the generated id
# never collides with ``_FRESH_TURN_ID``.
_TURN_ID_ALPHABET = st.characters(min_codepoint=0x21, max_codepoint=0x7E)
_TURN_ID_STRATEGY = st.text(alphabet=_TURN_ID_ALPHABET, min_size=1, max_size=24).map(lambda s: f"turn-{s}")


@hypothesis.given(persisted_turn_id=_TURN_ID_STRATEGY)
@hypothesis.example(persisted_turn_id="turn-A")
@hypothesis.settings(max_examples=25, deadline=None)
def test_prime_with_baseline_current_snapshot_preserves_persisted_cursor_when_snapshot_has_no_turn(
    persisted_turn_id: str,
) -> None:
    """Bug-condition exploration test for ``StructuredReplyPresenter.prime``.

    Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3

    Setup (mirrors design "Exploratory Bug Condition Checking"):
      1. Fake task service whose ``get_structured_reply_cursor`` returns
         ``(persisted_turn_id, None)`` (turn previously acknowledged).
      2. First snapshot has no turns (so ``snapshot.turn_id is None``).
      3. Second snapshot's latest completed assistant turn is the persisted
         (previously acknowledged) turn.
      4. Third snapshot adds a fresh ``FRESH-TURN-B`` completed assistant
         turn.

    Assertions:
      * After ``prime(baseline_current_snapshot=True)``,
        ``_last_structured_turn_id == persisted_turn_id`` (Bug Condition
        direct check; expected behavior).
      * The first ``poll`` does NOT emit any ``StructuredReplyOutput`` for
        ``persisted_turn_id`` (the previously acknowledged turn must not be
        replayed).
      * The second ``poll`` emits exactly one ``StructuredReplyOutput`` for
        the fresh turn ``FRESH-TURN-B``, and no ``StructuredReplyOutput``
        for ``persisted_turn_id`` is ever produced across the two polls.

    Expected outcome on UNFIXED code: this test FAILS. The unfixed
    ``prime`` sets ``_last_structured_turn_id = snapshot.turn_id`` which is
    ``None``, so the first ``poll`` re-emits ``StructuredReplyOutput(turn_id=
    persisted_turn_id, ...)`` -- the duplicate "stale reply" the user sees.
    """
    cursor_after_prime, first_poll, second_poll = asyncio.run(_run_bug_condition_scenario(persisted_turn_id))

    # 1) Bug Condition direct check (Requirements 2.1).
    assert cursor_after_prime == persisted_turn_id, (
        "prime(baseline_current_snapshot=True) discarded the persisted reply cursor when "
        f"snapshot.turn_id was None: expected _last_structured_turn_id == {persisted_turn_id!r}, "
        f"got {cursor_after_prime!r}. The previously acknowledged turn will be re-emitted as a "
        "duplicate stale reply on the next pump cycle."
    )

    # 2) The first pump cycle MUST NOT replay the previously acknowledged turn (Requirement 2.2).
    stale_emits_first_poll = [out for out in first_poll if isinstance(out, StructuredReplyOutput) and out.turn_id == persisted_turn_id]
    assert stale_emits_first_poll == [], (
        f"first poll re-emitted the previously acknowledged turn {persisted_turn_id!r} "
        f"as a StructuredReplyOutput: {stale_emits_first_poll!r}. This is the duplicate "
        "stale reply described in bugfix.md §1.2."
    )

    # 3) The fresh turn must be emitted exactly once on the second poll (Requirement 2.3).
    structured_emits_second_poll = [out for out in second_poll if isinstance(out, StructuredReplyOutput)]
    assert len(structured_emits_second_poll) == 1, (
        f"expected exactly one StructuredReplyOutput on the second poll, got {structured_emits_second_poll!r}"
    )
    assert structured_emits_second_poll[0].turn_id == _FRESH_TURN_ID, (
        f"second poll did not emit the fresh turn: {structured_emits_second_poll[0]!r}"
    )

    # 4) Across both polls combined, the previously acknowledged turn must never be emitted.
    stale_emits_total = [
        out for out in (*first_poll, *second_poll) if isinstance(out, StructuredReplyOutput) and out.turn_id == persisted_turn_id
    ]
    assert stale_emits_total == [], (
        f"the previously acknowledged turn {persisted_turn_id!r} was re-emitted across the two polls: {stale_emits_total!r}"
    )


# ---------------------------------------------------------------------------
# Property 2: Preservation
#
# For every input where the bug condition does NOT hold, ``prime()`` must
# leave ``_last_structured_turn_id`` (and the other observable side effects
# listed below) identical to the current (unfixed) implementation, so the
# upcoming one-line fallback fix introduces zero regressions.
#
# Bug Condition (excluded via ``hypothesis.assume``):
#     baseline_current_snapshot == True
#     AND snapshot_turn_id IS None
#     AND persisted_turn_id IS NOT None
#
# Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5
# ---------------------------------------------------------------------------

from app.domain.session_models import PendingPermission  # noqa: E402
from app.domain.user_question_models import (  # noqa: E402
    UserQuestionOption,
    UserQuestionPrompt,
)

_OPTIONAL_TURN_ID_STRATEGY = st.one_of(st.none(), _TURN_ID_STRATEGY)

# Permission keys are persisted as ``"<tool_use_id>:<tool_name>"``; we keep
# the alphabet narrow so generated values round-trip through logging.
_PERMISSION_KEY_ALPHABET = st.characters(min_codepoint=0x21, max_codepoint=0x7E, blacklist_characters=":")
_PERMISSION_KEY_BASE = st.text(alphabet=_PERMISSION_KEY_ALPHABET, min_size=1, max_size=12)
_OPTIONAL_PERMISSION_KEY_STRATEGY = st.one_of(
    st.none(),
    st.builds(lambda a, b: f"{a}:{b}", _PERMISSION_KEY_BASE, _PERMISSION_KEY_BASE),
)


class _PreservationTaskService:
    """Fake task service with fully controllable persisted cursors.

    Mirrors ``DummyTaskService``/``_DummyTaskServiceWithPersistedCursor`` but
    lets the test pin ``persisted_turn_id``, ``persisted_permission_key``,
    and ``persisted_question_key`` independently.
    """

    def __init__(
        self,
        sessions: list[object],
        *,
        persisted_turn_id: str | None,
        persisted_permission_key: str | None,
        persisted_question_key: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._index = 0
        self._persisted_turn_id = persisted_turn_id
        self._persisted_permission_key = persisted_permission_key
        self._persisted_question_key = persisted_question_key

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        if self._index >= len(self._sessions):
            return self._sessions[-1]
        session = self._sessions[self._index]
        self._index += 1
        return session

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return self._index

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        return self._persisted_turn_id, self._persisted_permission_key

    async def acknowledge_structured_reply(
        self,
        user_id: int,
        *,
        turn_id: str | None = None,
        permission_key: str | None = None,
        task_id: str | None = None,
    ) -> None:
        return None

    async def get_structured_user_question_cursor(self, user_id: int, *, task_id: str | None = None):
        return self._persisted_question_key

    async def acknowledge_structured_user_question(
        self,
        user_id: int,
        *,
        question_key: str | None = None,
        task_id: str | None = None,
    ) -> None:
        self._persisted_question_key = question_key

    async def wait_for_structured_session_update(
        self,
        *,
        user_id: int,
        since_cursor: int,
        timeout_sec: float,
        task_id: str | None = None,
    ) -> bool:
        return True


def _build_prime_session(snapshot_turn_id: str | None):
    """Build a session whose snapshot exposes ``snapshot.turn_id == snapshot_turn_id``."""
    if snapshot_turn_id is None:
        # Empty turns -> snapshot loader returns turn_id=None.
        return _session(phase=SessionPhase.WAITING_FOR_INPUT)
    return _session(
        phase=SessionPhase.WAITING_FOR_INPUT,
        turns=[
            ConversationTurn(
                turn_id=snapshot_turn_id,
                role="assistant",
                text="\nhello\n",
                is_complete=True,
            ),
        ],
    )


async def _prime_and_capture(
    *,
    baseline_current_snapshot: bool,
    snapshot_turn_id: str | None,
    persisted_turn_id: str | None,
    persisted_permission_key: str | None,
):
    service = _PreservationTaskService(
        sessions=[_build_prime_session(snapshot_turn_id)],
        persisted_turn_id=persisted_turn_id,
        persisted_permission_key=persisted_permission_key,
    )
    presenter = StructuredReplyPresenter(task_service=service, user_id=1)
    await presenter.prime(baseline_current_snapshot=baseline_current_snapshot)
    return presenter


@hypothesis.given(
    baseline_current_snapshot=st.booleans(),
    snapshot_turn_id=_OPTIONAL_TURN_ID_STRATEGY,
    persisted_turn_id=_OPTIONAL_TURN_ID_STRATEGY,
    persisted_permission_key=_OPTIONAL_PERMISSION_KEY_STRATEGY,
)
@hypothesis.example(  # Cold start: baseline=True, snapshot=None, persisted=None.
    baseline_current_snapshot=True,
    snapshot_turn_id=None,
    persisted_turn_id=None,
    persisted_permission_key=None,
)
@hypothesis.example(  # Snapshot-has-turn with arbitrary persisted cursor.
    baseline_current_snapshot=True,
    snapshot_turn_id="turn-S",
    persisted_turn_id="turn-A",
    persisted_permission_key="tool-1:Bash",
)
@hypothesis.example(  # Non-baseline path: baseline=False, snapshot=None, persisted=None.
    baseline_current_snapshot=False,
    snapshot_turn_id=None,
    persisted_turn_id=None,
    persisted_permission_key=None,
)
@hypothesis.example(  # Non-baseline path: snapshot has turn, persisted differs.
    baseline_current_snapshot=False,
    snapshot_turn_id="turn-S",
    persisted_turn_id="turn-A",
    persisted_permission_key=None,
)
@hypothesis.settings(max_examples=60, deadline=None)
def test_prime_preserves_last_structured_turn_id_for_non_bug_inputs(
    baseline_current_snapshot: bool,
    snapshot_turn_id: str | None,
    persisted_turn_id: str | None,
    persisted_permission_key: str | None,
) -> None:
    """Preservation of ``_last_structured_turn_id`` for non-bug inputs.

    Validates: Requirements 3.1, 3.2

    Filters out the bug condition with ``hypothesis.assume``. The oracle
    mirrors the unfixed (and fixed) assignment so that:

    - ``baseline=True`` AND ``snapshot.turn_id`` non-``None`` ->
      ``_last_structured_turn_id == snapshot.turn_id`` (Req 3.1).
    - ``baseline=False`` -> ``_last_structured_turn_id == persisted_turn_id``
      (Req 3.2).
    - ``baseline=True`` AND ``snapshot.turn_id is None`` AND
      ``persisted_turn_id is None`` -> ``_last_structured_turn_id is None``
      (cold-start preservation; pins the falsy fallback semantics).
    """
    # Exclude the bug condition: it is covered by Property 1.
    hypothesis.assume(not (baseline_current_snapshot and snapshot_turn_id is None and persisted_turn_id is not None))

    presenter = asyncio.run(
        _prime_and_capture(
            baseline_current_snapshot=baseline_current_snapshot,
            snapshot_turn_id=snapshot_turn_id,
            persisted_turn_id=persisted_turn_id,
            persisted_permission_key=persisted_permission_key,
        )
    )

    if baseline_current_snapshot:
        # Non-bug baseline branch: either the snapshot already has a turn
        # (the oracle is ``snapshot.turn_id``) or both inputs are ``None``
        # (cold-start case, oracle is ``None``).
        if snapshot_turn_id is not None:
            assert presenter._last_structured_turn_id == snapshot_turn_id, (
                f"baseline=True, snapshot.turn_id={snapshot_turn_id!r}: expected "
                f"_last_structured_turn_id == {snapshot_turn_id!r}, got "
                f"{presenter._last_structured_turn_id!r}"
            )
        else:
            # snapshot_turn_id is None AND (by assume) persisted_turn_id is None.
            assert presenter._last_structured_turn_id is None, (
                "cold start (baseline=True, snapshot.turn_id=None, persisted_turn_id=None) "
                f"should leave _last_structured_turn_id=None, got "
                f"{presenter._last_structured_turn_id!r}"
            )
    else:
        # Non-baseline branch: persisted cursor is restored verbatim.
        assert presenter._last_structured_turn_id == persisted_turn_id, (
            f"baseline=False: expected _last_structured_turn_id == {persisted_turn_id!r}, got {presenter._last_structured_turn_id!r}"
        )


@hypothesis.given(
    baseline_current_snapshot=st.booleans(),
    snapshot_turn_id=_OPTIONAL_TURN_ID_STRATEGY,
    persisted_turn_id=_OPTIONAL_TURN_ID_STRATEGY,
    persisted_permission_key=_OPTIONAL_PERMISSION_KEY_STRATEGY,
)
@hypothesis.settings(max_examples=40, deadline=None)
def test_prime_preserves_persisted_permission_key_and_snapshot_observables(
    baseline_current_snapshot: bool,
    snapshot_turn_id: str | None,
    persisted_turn_id: str | None,
    persisted_permission_key: str | None,
) -> None:
    """Preservation of permission/session/phase observables for non-bug inputs.

    Validates: Requirements 3.5

    For all non-bug inputs, ``prime()`` must:

    - Set ``_last_pending_permission_key`` to the persisted permission key
      (regardless of ``baseline_current_snapshot``).
    - Mark ``_structured_session_available = True`` whenever a session was
      loaded.
    - Set ``_current_session_id`` to the snapshot's session id
      (``"claude-session-1"`` in this fixture).
    - Set ``_last_phase`` to the snapshot's phase
      (``SessionPhase.WAITING_FOR_INPUT.value``).
    """
    hypothesis.assume(not (baseline_current_snapshot and snapshot_turn_id is None and persisted_turn_id is not None))

    presenter = asyncio.run(
        _prime_and_capture(
            baseline_current_snapshot=baseline_current_snapshot,
            snapshot_turn_id=snapshot_turn_id,
            persisted_turn_id=persisted_turn_id,
            persisted_permission_key=persisted_permission_key,
        )
    )

    # Permission key cursor preservation (Req 3.5).
    assert presenter._last_pending_permission_key == persisted_permission_key, (
        f"expected _last_pending_permission_key == {persisted_permission_key!r}, got {presenter._last_pending_permission_key!r}"
    )
    # Session/phase observables reflect the loaded snapshot (Req 3.5).
    assert presenter._structured_session_available is True
    assert presenter._current_session_id == "claude-session-1"
    assert presenter._last_phase == SessionPhase.WAITING_FOR_INPUT.value


def test_prime_with_baseline_seeds_user_question_cursor_from_first_pending_prompt() -> None:
    """User-question cursor seeding when baseline=True and persisted cursor is None.

    Validates: Requirements 3.4

    When the persisted user-question cursor is ``None`` and the snapshot
    contains a pending ``AskUserQuestion`` tool with at least one prompt,
    ``prime(baseline_current_snapshot=True)`` must seed the tracker cursor
    with the first pending prompt's key.
    """
    pending = PendingPermission(
        tool_use_id="tool-ask-pending",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {
                    "header": "出发日期",
                    "question": "你想查哪一天出发？",
                    "options": [
                        {"label": "今天", "description": "查询今天"},
                        {"label": "明天", "description": "查询明天"},
                    ],
                    "multiSelect": False,
                },
                {
                    "header": "出发站",
                    "question": "从哪个站出发？",
                    "options": [
                        {"label": "郑州站", "description": "只查询郑州站"},
                        {"label": "都查", "description": "查询所有相关站"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
    )
    expected_first_prompt_key = UserQuestionPrompt(
        tool_use_id="tool-ask-pending",
        question_index=0,
        total_questions=2,
        header="出发日期",
        question="你想查哪一天出发？",
        options=(
            UserQuestionOption(label="今天", description="查询今天"),
            UserQuestionOption(label="明天", description="查询明天"),
        ),
        multi_select=False,
    ).key

    service = _PreservationTaskService(
        sessions=[_session(phase=SessionPhase.WAITING_FOR_APPROVAL, pending=pending)],
        persisted_turn_id=None,
        persisted_permission_key=None,
        persisted_question_key=None,
    )
    presenter = StructuredReplyPresenter(task_service=service, user_id=1)

    asyncio.run(presenter.prime(baseline_current_snapshot=True))

    assert presenter._user_question_tracker.last_question_key == expected_first_prompt_key, (
        "baseline=True with no persisted question cursor and a pending AskUserQuestion "
        f"tool should seed the cursor with the first prompt's key ({expected_first_prompt_key!r}), "
        f"got {presenter._user_question_tracker.last_question_key!r}"
    )


def test_prime_without_baseline_does_not_seed_user_question_cursor_from_pending_prompts() -> None:
    """Non-baseline path leaves the user-question cursor untouched (None).

    Validates: Requirements 3.2, 3.4

    ``baseline_current_snapshot=False`` must NOT seed the question cursor
    from pending prompts, even if the snapshot exposes a pending
    ``AskUserQuestion``. This documents the unchanged ``baseline=False``
    behavior we must preserve.
    """
    pending = PendingPermission(
        tool_use_id="tool-ask-pending",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {
                    "header": "出发日期",
                    "question": "你想查哪一天出发？",
                    "options": [
                        {"label": "今天", "description": "查询今天"},
                        {"label": "明天", "description": "查询明天"},
                    ],
                    "multiSelect": False,
                },
            ]
        },
    )
    service = _PreservationTaskService(
        sessions=[_session(phase=SessionPhase.WAITING_FOR_APPROVAL, pending=pending)],
        persisted_turn_id=None,
        persisted_permission_key=None,
        persisted_question_key=None,
    )
    presenter = StructuredReplyPresenter(task_service=service, user_id=1)

    asyncio.run(presenter.prime(baseline_current_snapshot=False))

    assert presenter._user_question_tracker.last_question_key is None, (
        "baseline=False must NOT seed the user-question cursor from pending prompts; "
        f"got {presenter._user_question_tracker.last_question_key!r}"
    )
