"""Bug-condition exploration tests for ``StructuredReplyPresenter._collect_reply``.

Spec: ``.kiro/specs/presenter-replay-pre-task-turn-after-bot-restart``.

These tests pin **Property 1** from the design (the bug condition):

    Bug Condition (from design ``isBugCondition``):
        task_started_at IS NOT None
        AND snapshot.turn_id IS NOT None
        AND snapshot.turn_ended_at IS NOT None
        AND snapshot.turn_ended_at < task_started_at
        AND snapshot.turn_id != _last_structured_turn_id

    Expected Behavior (from design ``expectedBehavior``):
        After the corresponding ``_collect_reply`` call (driven via
        ``await presenter.poll(task_id="task-1")``), no
        ``StructuredReplyOutput`` whose ``turn_id == snapshot.turn_id`` is
        returned AND ``presenter._last_structured_turn_id`` is unchanged.

The test is expected to **fail on the unfixed code**: the current
``_collect_reply`` (``app/bot/presenters/structured_reply_presenter.py``)
has no time-window guard, so after a bot restart with a still-running
Claude tmux session — where the persisted reply cursor is ``None``
(``SessionState`` rehydrated fresh) and the in-memory cursor is also
``None`` (the previous bugfix's ``or persisted_turn_id`` fallback collapses
to ``None``) — the next pump cycle sees the most-recent pre-restart
assistant turn as "new" and emits it as a ``StructuredReplyOutput``. The
genuine reply to the new user message arrives a moment later as a second
``StructuredReplyOutput``, producing two Telegram bubbles where the user
expected one.

Counterexample observed on UNFIXED code (with
``delta_pre=timedelta(seconds=1), turn_id="pre-A"``):

    First ``await presenter.poll(task_id="task-1")`` (snapshot exposes only
    the pre-task turn ``A``):
        [StructuredReplyOutput(text="stale pre-restart reply", turn_id="pre-A")]
        # BUG: expected []  (pre-task turn must be skipped)

    Second ``await presenter.poll(task_id="task-1")`` (snapshot exposes the
    pre-task turn ``A`` AND the fresh post-task turn ``B``):
        [StructuredReplyOutput(text="fresh reply", turn_id="post-task-B")]
        # OK on its own, but the first poll's emission of ``pre-A`` already
        # confirmed the duplicate stale-reply bubble described in
        # ``bugfix.md`` §1.2 / §1.3.

The combined sequence on UNFIXED code is therefore:
``[StructuredReplyOutput(turn_id="pre-A"),
   StructuredReplyOutput(turn_id="post-task-B")]`` — the two bubbles the
user sees on Telegram after a bot restart, instead of the single fresh
reply.

Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import hypothesis
import hypothesis.strategies as st
import pytest  # noqa: F401  # imported so pytest-asyncio plugin is loaded for sibling files

from app.bot.presenters.structured_reply_presenter import (
    StructuredReplyOutput,
    StructuredReplyPresenter,
)
from app.domain.session_models import ConversationTurn, SessionPhase
from tests.fakes.structured import make_structured_session as _session


# Fixed wall-clock anchor for the current task's ``started_at``. Mirrors the
# production trace timestamp from ``bugfix.md`` (the second message
# emitted at ``2026-05-19T16:58:25Z`` was the genuine reply; the task's
# ``started_at`` is the immediately preceding second).
_FIXED_TASK_STARTED_AT = datetime(2026, 5, 19, 16, 58, 24, tzinfo=timezone.utc)
# Fresh post-task turn id and ended_at. Two seconds AFTER the task started
# guarantees ``B.ended_at >= task_started_at`` so the new pre-task guard does
# NOT suppress ``B`` once it lands.
_FRESH_TURN_B_ID = "post-task-B"
_FRESH_TURN_B_ENDED_AT = _FIXED_TASK_STARTED_AT + timedelta(seconds=2)


class _DummyTaskServiceWithEmptyPersistedCursor:
    """Fake task service for the post-bot-restart shape.

    Mirrors ``DummyTaskService`` from
    ``tests/test_structured_reply_presenter.py`` and
    ``_DummyTaskServiceWithPersistedCursor`` from
    ``tests/test_structured_reply_presenter_prime.py``, but
    ``get_structured_reply_cursor`` returns ``(None, None)`` to model the
    post-restart state where ``SessionState`` was rehydrated fresh
    (``structured_reply_turn_id=None``).
    """

    def __init__(self, sessions: list[object]) -> None:
        self._sessions = sessions
        self._index = 0

    async def get_structured_session(self, user_id: int, *, log_missing: bool = True):
        if self._index >= len(self._sessions):
            return self._sessions[-1]
        session = self._sessions[self._index]
        self._index += 1
        return session

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True):
        # Delegate to the same advancing-index sequence so the snapshot
        # loader sees one session per call regardless of whether the
        # presenter was constructed with a task_id.
        return await self.get_structured_session(user_id, log_missing=log_missing)

    async def get_structured_session_cursor(self, user_id: int, *, task_id: str | None = None) -> int:
        return self._index

    async def get_structured_reply_cursor(self, user_id: int, *, task_id: str | None = None):
        # Post-restart shape: nothing persisted, no in-memory cursor either.
        return None, None

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


async def _run_pre_task_replay_scenario(*, delta_pre: timedelta, turn_id: str):
    """Drive the post-bot-restart bug-condition scenario.

    Returns ``(cursor_after_prime, first_poll, second_poll)``.
    """
    pre_task_ended_at = _FIXED_TASK_STARTED_AT - delta_pre
    pre_task_turn = ConversationTurn(
        turn_id=turn_id,
        role="assistant",
        text="\nstale pre-restart reply\n",
        is_complete=True,
        ended_at=pre_task_ended_at,
    )
    fresh_turn = ConversationTurn(
        turn_id=_FRESH_TURN_B_ID,
        role="assistant",
        text="\nfresh reply\n",
        is_complete=True,
        ended_at=_FRESH_TURN_B_ENDED_AT,
    )

    sessions = [
        # 1) Prime-time snapshot: no turns yet (post-restart fresh state).
        #    The snapshot loader returns ``turn_id=None`` so ``prime`` leaves
        #    ``_last_structured_turn_id`` as ``None`` (both the in-memory
        #    cursor and the persisted cursor are empty).
        _session(phase=SessionPhase.WAITING_FOR_INPUT),
        # 2) Next-poll snapshot: only the pre-task turn ``A`` is surfaced by
        #    the loader. ``A.ended_at < task_started_at`` triggers the bug
        #    condition on UNFIXED code.
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[pre_task_turn],
        ),
        # 3) Subsequent snapshot: the genuine reply ``B`` to the new user
        #    message has now completed. The loader picks ``B`` (latest
        #    completed assistant turn in reverse iteration order). On the
        #    fixed code, ``B.ended_at >= task_started_at`` so the new guard
        #    does NOT suppress ``B``.
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[pre_task_turn, fresh_turn],
        ),
    ]

    service = _DummyTaskServiceWithEmptyPersistedCursor(sessions)
    presenter = StructuredReplyPresenter(task_service=service, user_id=1, task_id="task-1")

    # Hack for testing pre-fix code: the unfixed ``StructuredReplyPresenter``
    # constructor does not yet accept ``task_started_at`` (introduced by
    # task 3.3) and ``_StructuredSnapshot`` does not yet expose
    # ``turn_ended_at`` (introduced by tasks 3.1/3.2). We set the attribute
    # directly on the instance so:
    #   - On the FIXED code, ``_collect_reply``'s new guard reads
    #     ``self._task_started_at`` from this attribute (the constructor
    #     would normally seed it; the manual assignment is equivalent).
    #   - On the UNFIXED code, the attribute simply has no consumer (no
    #     guard reads it), so the bug still manifests because there is no
    #     time-window check at all and the pre-task turn ``A`` is emitted.
    presenter._task_started_at = _FIXED_TASK_STARTED_AT  # type: ignore[attr-defined]

    await presenter.prime(baseline_current_snapshot=True)
    cursor_after_prime = presenter._last_structured_turn_id

    first_poll = await presenter.poll(task_id="task-1")
    second_poll = await presenter.poll(task_id="task-1")
    return cursor_after_prime, first_poll, second_poll


# Restrict turn ids to printable ASCII (no whitespace, no control chars) so
# generated values round-trip cleanly through dataclasses, logging extras,
# and ``normalize_stream_text``. The ``pre-`` prefix guarantees the
# generated id never collides with ``_FRESH_TURN_B_ID``.
_TURN_ID_ALPHABET = st.characters(min_codepoint=0x21, max_codepoint=0x7E)
_PRE_TASK_TURN_ID_STRATEGY = st.text(alphabet=_TURN_ID_ALPHABET, min_size=1, max_size=24).map(lambda s: f"pre-{s}")
# Strict positive deltas: a turn that ended at exactly ``task_started_at`` is
# NOT a pre-task turn (the design uses a strict ``<`` predicate).
_PRE_TASK_DELTA_STRATEGY = st.timedeltas(
    min_value=timedelta(seconds=1),
    max_value=timedelta(days=1),
)


@hypothesis.given(
    delta_pre=_PRE_TASK_DELTA_STRATEGY,
    turn_id=_PRE_TASK_TURN_ID_STRATEGY,
)
@hypothesis.example(delta_pre=timedelta(seconds=1), turn_id="pre-A")
@hypothesis.settings(max_examples=25, deadline=None)
def test_collect_reply_skips_pre_task_turn_after_bot_restart(
    delta_pre: timedelta,
    turn_id: str,
) -> None:
    """Bug-condition exploration test for ``StructuredReplyPresenter._collect_reply``.

    Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3

    Setup (mirrors design "Exploratory Bug Condition Checking"):
      1. Fake task service whose ``get_structured_reply_cursor`` returns
         ``(None, None)`` (post-restart fresh state).
      2. Prime-time snapshot has no turns (``snapshot.turn_id is None``);
         after ``prime(baseline_current_snapshot=True)`` both
         ``_last_structured_turn_id`` and the persisted cursor are ``None``.
      3. Next-poll snapshot's only completed assistant turn ``A`` is
         pre-task: ``A.ended_at = task_started_at - delta_pre`` for
         ``delta_pre`` drawn from ``[1s, 1d]``.
      4. Subsequent snapshot contains both ``A`` and a fresh post-task turn
         ``B`` whose ``ended_at = task_started_at + 2s``.

    Expected post-fix behavior:
      * ``_last_structured_turn_id`` is ``None`` after ``prime``.
      * The first ``poll`` does NOT emit any ``StructuredReplyOutput`` for
        the pre-task ``turn_id`` — the new pre-task guard suppresses ``A``
        and leaves ``_last_structured_turn_id`` unchanged.
      * The second ``poll`` emits exactly one ``StructuredReplyOutput`` for
        ``_FRESH_TURN_B_ID`` and never emits ``A``.

    Expected outcome on UNFIXED code: this test FAILS. The unfixed
    ``_collect_reply`` has no time-window guard, so the first poll re-emits
    the pre-task turn ``A`` as ``StructuredReplyOutput(text="stale
    pre-restart reply", turn_id=A)`` — the duplicate Telegram bubble the
    user sees in chat mode.

    Counterexample reproduced via
    ``@example(delta_pre=timedelta(seconds=1), turn_id="pre-A")``: the
    first poll on the unfixed code returns
    ``[StructuredReplyOutput(text="stale pre-restart reply",
    turn_id="pre-A")]`` instead of ``[]``.
    """
    cursor_after_prime, first_poll, second_poll = asyncio.run(_run_pre_task_replay_scenario(delta_pre=delta_pre, turn_id=turn_id))

    # 1) Sanity: post-restart prime leaves the in-memory cursor empty
    #    because both ``snapshot.turn_id`` and the persisted cursor are
    #    ``None``. This pins the precondition for the bug-condition shape.
    assert cursor_after_prime is None, f"expected _last_structured_turn_id == None after post-restart prime, got {cursor_after_prime!r}"

    # 2) The first pump cycle MUST NOT replay the pre-task turn A
    #    (Requirement 2.1). On the UNFIXED code this assertion FAILS with
    #    a ``StructuredReplyOutput(turn_id=A)`` in the list, demonstrating
    #    the bug.
    pre_task_emits_first_poll = [out for out in first_poll if isinstance(out, StructuredReplyOutput) and out.turn_id == turn_id]
    assert pre_task_emits_first_poll == [], (
        f"first poll re-emitted the pre-task turn {turn_id!r} as a "
        f"StructuredReplyOutput: {pre_task_emits_first_poll!r}. This is "
        "the duplicate stale-reply bubble described in bugfix.md §1.2."
    )

    # 3) The fresh post-task turn ``B`` must be emitted exactly once on the
    #    second poll (Requirement 2.2).
    structured_emits_second_poll = [out for out in second_poll if isinstance(out, StructuredReplyOutput)]
    assert len(structured_emits_second_poll) == 1, (
        f"expected exactly one StructuredReplyOutput on the second poll, got {structured_emits_second_poll!r}"
    )
    assert structured_emits_second_poll[0].turn_id == _FRESH_TURN_B_ID, (
        f"second poll did not emit the fresh post-task turn: {structured_emits_second_poll[0]!r}"
    )

    # 4) Across both polls combined, the pre-task turn must never be
    #    emitted (Requirement 2.3 -- direct check that the bug condition is
    #    fully suppressed end-to-end).
    pre_task_emits_total = [out for out in (*first_poll, *second_poll) if isinstance(out, StructuredReplyOutput) and out.turn_id == turn_id]
    assert pre_task_emits_total == [], f"the pre-task turn {turn_id!r} was re-emitted across the two polls: {pre_task_emits_total!r}"


# ============================================================================
# Property 2: Preservation -- Non-Bug Inputs Match Original Behavior
#
# Bug Condition (excluded by construction in each test):
#     task_started_at IS NOT None
#     AND snapshot.turn_id IS NOT None
#     AND snapshot.turn_ended_at IS NOT None
#     AND snapshot.turn_ended_at < task_started_at
#     AND snapshot.turn_id != _last_structured_turn_id
#
# Each test below constructs scenarios where AT LEAST ONE clause is FALSE
# and asserts the presenter behaves identically to the unfixed
# implementation. The new guard (post-fix) is a no-op for every input
# below, so all tests pass on UNFIXED code and continue to pass after the
# fix in tasks 3.1-3.4 lands.
#
# Forward-compatibility notes (from design "Test Plan"):
#   * The unfixed ``_StructuredSnapshot`` has no ``turn_ended_at`` field;
#     the unfixed loader simply ignores ``ConversationTurn.ended_at``. We
#     construct ``ConversationTurn`` instances with ``ended_at`` set so
#     the FIXED loader can populate ``snapshot.turn_ended_at`` after the
#     fix lands; on UNFIXED code the field is absent and the new guard
#     can never evaluate.
#   * Tests that need ``task_started_at != None`` set it via direct
#     attribute assignment on the presenter instance (the unfixed
#     ``__init__`` does not yet accept the keyword argument; setting the
#     attribute is a no-op on unfixed code and equivalent to the
#     constructor on fixed code).
#
# Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
# ============================================================================


_PRESERVE_TURN_ID_STRATEGY = st.text(alphabet=_TURN_ID_ALPHABET, min_size=1, max_size=24).map(lambda s: f"keep-{s}")
# Persisted-turn-id strategy uses a distinct prefix so the generated value
# never collides with ``_FRESH_TURN_B_ID``, ``pre-...``, or ``keep-...``.
_PERSISTED_TURN_ID_STRATEGY = st.text(alphabet=_TURN_ID_ALPHABET, min_size=1, max_size=24).map(lambda s: f"persist-{s}")
# Post-task delta: zero is the boundary equality case (>= T), positive is
# strictly after T. Both must be emitted (strict ``<`` predicate).
_POST_TASK_DELTA_STRATEGY = st.timedeltas(
    min_value=timedelta(seconds=0),
    max_value=timedelta(days=1),
)


class _PreservationTaskService:
    """Configurable fake task service for preservation tests.

    Mirrors ``_DummyTaskServiceWithEmptyPersistedCursor`` above and the
    ``_PreservationTaskService`` from
    ``tests/test_structured_reply_presenter_prime.py``, but lets each test
    pin ``persisted_turn_id``, ``persisted_permission_key``, and
    ``persisted_question_key`` independently.
    """

    def __init__(
        self,
        sessions: list[object],
        *,
        persisted_turn_id: str | None = None,
        persisted_permission_key: str | None = None,
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

    async def get_structured_session_for_task(self, *, task_id: str, user_id: int, log_missing: bool = True):
        return await self.get_structured_session(user_id, log_missing=log_missing)

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


def _assistant_turn(turn_id: str, *, ended_at: datetime | None, text: str = "\nfresh reply\n") -> ConversationTurn:
    return ConversationTurn(
        turn_id=turn_id,
        role="assistant",
        text=text,
        is_complete=True,
        ended_at=ended_at,
    )


# ---- Test 1: No task context preserves all behavior -------------------------


async def _run_no_task_context_scenario(*, turn_id: str, ended_at: datetime):
    sessions = [
        # 1) prime: empty session, snapshot.turn_id is None.
        _session(phase=SessionPhase.WAITING_FOR_INPUT),
        # 2) first poll: snapshot exposes the new turn.
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[_assistant_turn(turn_id, ended_at=ended_at)],
        ),
        # 3) second poll: same turn -- duplicate-turn guard suppresses
        #    (the first poll's emission is acknowledged below so
        #    ``_last_structured_turn_id`` advances).
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[_assistant_turn(turn_id, ended_at=ended_at)],
        ),
    ]
    service = _PreservationTaskService(sessions)
    presenter = StructuredReplyPresenter(task_service=service, user_id=1, task_id="task-1")
    # task_started_at left unset: on unfixed code there is no attribute; on
    # fixed code the constructor default is None. The new guard cannot fire
    # in either case (its first clause is ``task_started_at is not None``).

    await presenter.prime(baseline_current_snapshot=True)
    cursor_after_prime = presenter._last_structured_turn_id
    first_poll = await presenter.poll(task_id="task-1")
    # Mirror the production pump pattern: acknowledge the emitted reply so
    # the in-memory cursor advances and the duplicate-turn guard fires on
    # the next poll. This matches how ``RunEventStreamer`` drives the
    # presenter and the established pattern in
    # ``tests/test_structured_reply_presenter.py``.
    for output in first_poll:
        if isinstance(output, StructuredReplyOutput):
            await presenter.acknowledge_delivery(output)
    second_poll = await presenter.poll(task_id="task-1")
    return cursor_after_prime, first_poll, second_poll


@hypothesis.given(
    delta_seconds=st.integers(min_value=-86400, max_value=86400),
    turn_id=_PRESERVE_TURN_ID_STRATEGY,
)
@hypothesis.example(delta_seconds=-3600, turn_id="keep-pre")
@hypothesis.example(delta_seconds=0, turn_id="keep-zero")
@hypothesis.example(delta_seconds=10, turn_id="keep-post")
@hypothesis.settings(max_examples=20, deadline=None)
def test_preserve_no_task_context_emits_for_any_ended_at(delta_seconds: int, turn_id: str) -> None:
    """``task_started_at=None`` -> standard emission path runs unchanged.

    Validates: Requirements 3.7

    With no task context (presenter constructed without ``task_started_at``,
    or with ``task_started_at=None``), the new pre-task guard is a no-op
    for every snapshot regardless of the turn's ``ended_at``. The presenter
    must behave exactly as the unfixed implementation:

      * ``prime`` with an empty snapshot leaves
        ``_last_structured_turn_id`` as ``None`` (cold-start preservation).
      * The first poll emits the fresh turn exactly once.
      * A subsequent poll on the same turn is suppressed by the existing
        ``snapshot.turn_id == _last_structured_turn_id`` duplicate-turn
        guard.

    Hypothesis covers ``delta_seconds`` from one day BEFORE to one day
    AFTER the fixed task anchor; this is the "any-ended-at" preservation
    property.
    """
    ended_at = _FIXED_TASK_STARTED_AT + timedelta(seconds=delta_seconds)
    cursor_after_prime, first_poll, second_poll = asyncio.run(_run_no_task_context_scenario(turn_id=turn_id, ended_at=ended_at))

    assert cursor_after_prime is None, f"prime with empty snapshot should leave _last_structured_turn_id=None, got {cursor_after_prime!r}"
    structured_first = [o for o in first_poll if isinstance(o, StructuredReplyOutput)]
    assert len(structured_first) == 1, f"first poll must emit exactly one StructuredReplyOutput, got {structured_first!r}"
    assert structured_first[0].turn_id == turn_id
    structured_second = [o for o in second_poll if isinstance(o, StructuredReplyOutput)]
    assert structured_second == [], (
        f"second poll must suppress the duplicate turn via the existing duplicate-turn guard, got {structured_second!r}"
    )


# ---- Test 2: Snapshot has no turn_ended_at (mirrors unfixed-shape) ----------


def test_preserve_snapshot_without_turn_ended_at_emits_when_task_started_at_set() -> None:
    """``turn_ended_at=None`` short-circuits the new guard.

    Validates: Requirements 3.4

    On unfixed code, the snapshot loader does not populate
    ``turn_ended_at`` (the field doesn't exist on ``_StructuredSnapshot``).
    The fixed code's new guard requires both ``task_started_at`` AND
    ``turn_ended_at`` to be non-``None``; when ``turn_ended_at is None``
    the guard is a no-op even if ``task_started_at`` is set. Concretely:
    build a ``ConversationTurn`` whose ``ended_at=None`` (mid-stream / the
    loader did not record one) and assert the presenter emits it normally.
    """
    turn_id = "keep-no-ended-at"
    sessions = [
        _session(phase=SessionPhase.WAITING_FOR_INPUT),
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[
                ConversationTurn(
                    turn_id=turn_id,
                    role="assistant",
                    text="\nhello\n",
                    is_complete=True,
                    # ended_at left at default (None) -- forces snapshot.turn_ended_at=None
                    # on the FIXED loader; the UNFIXED loader ignores ended_at anyway.
                ),
            ],
        ),
    ]
    service = _PreservationTaskService(sessions)
    presenter = StructuredReplyPresenter(task_service=service, user_id=1, task_id="task-1")
    presenter._task_started_at = _FIXED_TASK_STARTED_AT  # type: ignore[attr-defined]

    asyncio.run(presenter.prime(baseline_current_snapshot=True))
    first_poll = asyncio.run(presenter.poll(task_id="task-1"))

    structured = [o for o in first_poll if isinstance(o, StructuredReplyOutput)]
    assert len(structured) == 1, f"turn with ended_at=None must be emitted (new guard short-circuits), got {structured!r}"
    assert structured[0].turn_id == turn_id


# ---- Test 3: Post-task turn (turn_ended_at >= task_started_at) -------------


@hypothesis.given(
    delta_post=_POST_TASK_DELTA_STRATEGY,
    turn_id=_PRESERVE_TURN_ID_STRATEGY,
)
@hypothesis.example(delta_post=timedelta(seconds=2), turn_id="keep-post")
@hypothesis.example(delta_post=timedelta(days=1), turn_id="keep-far-post")
@hypothesis.settings(max_examples=20, deadline=None)
def test_preserve_post_task_turn_emits_once_when_task_started_at_set(delta_post: timedelta, turn_id: str) -> None:
    """Post-task turn (``ended_at >= task_started_at``) is emitted exactly once.

    Validates: Requirements 3.4

    The new guard's predicate is strict ``turn_ended_at < task_started_at``.
    For ``delta_post >= 0``, ``(T + delta_post) < T`` is always false, so
    the guard never fires. The standard emission path runs.
    """
    ended_at = _FIXED_TASK_STARTED_AT + delta_post
    sessions = [
        _session(phase=SessionPhase.WAITING_FOR_INPUT),
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[_assistant_turn(turn_id, ended_at=ended_at)],
        ),
    ]
    service = _PreservationTaskService(sessions)
    presenter = StructuredReplyPresenter(task_service=service, user_id=1, task_id="task-1")
    presenter._task_started_at = _FIXED_TASK_STARTED_AT  # type: ignore[attr-defined]

    asyncio.run(presenter.prime(baseline_current_snapshot=True))
    first_poll = asyncio.run(presenter.poll(task_id="task-1"))

    structured = [o for o in first_poll if isinstance(o, StructuredReplyOutput)]
    assert len(structured) == 1, f"post-task turn (delta_post={delta_post!r}) must emit exactly once, got {structured!r}"
    assert structured[0].turn_id == turn_id


# ---- Test 4: Boundary equality (strict `<` predicate) ----------------------


def test_preserve_boundary_equality_emits_turn_when_ended_at_equals_task_started_at() -> None:
    """``ended_at == task_started_at`` -> turn IS emitted (strict ``<`` predicate).

    Validates: Requirements 3.4 (boundary case from design Risk Assessment)

    The new guard uses strict ``<``: a turn that completed in the same
    instant as the task started is NOT skipped. This is the safer side --
    a turn at exactly ``task_started_at`` is almost certainly the new turn
    or a tightly-scoped race we should not suppress.
    """
    turn_id = "keep-boundary"
    sessions = [
        _session(phase=SessionPhase.WAITING_FOR_INPUT),
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[_assistant_turn(turn_id, ended_at=_FIXED_TASK_STARTED_AT)],
        ),
    ]
    service = _PreservationTaskService(sessions)
    presenter = StructuredReplyPresenter(task_service=service, user_id=1, task_id="task-1")
    presenter._task_started_at = _FIXED_TASK_STARTED_AT  # type: ignore[attr-defined]

    asyncio.run(presenter.prime(baseline_current_snapshot=True))
    first_poll = asyncio.run(presenter.poll(task_id="task-1"))

    structured = [o for o in first_poll if isinstance(o, StructuredReplyOutput)]
    assert len(structured) == 1, f"boundary turn (ended_at == task_started_at) must emit exactly once, got {structured!r}"
    assert structured[0].turn_id == turn_id


# ---- Test 5: Previous bugfix preserved (presenter-duplicate-stale-reply) ---


@hypothesis.given(persisted_turn_id=_PERSISTED_TURN_ID_STRATEGY)
@hypothesis.example(persisted_turn_id="persist-A")
@hypothesis.settings(max_examples=15, deadline=None)
def test_preserve_previous_bugfix_when_baseline_and_snapshot_turn_id_is_none(
    persisted_turn_id: str,
) -> None:
    """Previous bugfix's ``or persisted_turn_id`` fallback in ``prime()`` stays intact.

    Validates: Requirements 3.1, 3.2

    Pins the EXACT shape covered by the previous bugfix
    (``presenter-duplicate-stale-reply``):

      * After ``prime(baseline=True)`` with ``snapshot.turn_id=None`` AND a
        non-``None`` persisted cursor, ``_last_structured_turn_id ==
        persisted_turn_id``.
      * On the next poll where the snapshot surfaces ``persisted_turn_id``,
        no ``StructuredReplyOutput`` is emitted (the existing duplicate-turn
        guard short-circuits BEFORE the new pre-task guard runs).

    This test must keep passing on UNFIXED code (where the previous bugfix
    is already in place) and on FIXED code (where the new guard is layered
    AFTER the duplicate-turn guard, so the duplicate guard wins).
    """
    sessions = [
        # 1) prime: empty turns -> snapshot.turn_id is None.
        _session(phase=SessionPhase.WAITING_FOR_INPUT),
        # 2) next poll: persisted turn surfaces in the snapshot. Use
        #    ended_at = task_started_at (boundary, NOT pre-task) so the
        #    test does not accidentally exercise the bug condition; the
        #    duplicate-turn guard fires first regardless.
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[
                _assistant_turn(persisted_turn_id, ended_at=_FIXED_TASK_STARTED_AT),
            ],
        ),
    ]
    service = _PreservationTaskService(sessions, persisted_turn_id=persisted_turn_id)
    presenter = StructuredReplyPresenter(task_service=service, user_id=1, task_id="task-1")
    # task_started_at left unset: the duplicate-turn guard runs BEFORE the
    # new pre-task guard, so this test is agnostic to whether
    # task_started_at is set.

    asyncio.run(presenter.prime(baseline_current_snapshot=True))
    cursor_after_prime = presenter._last_structured_turn_id
    first_poll = asyncio.run(presenter.poll(task_id="task-1"))

    assert cursor_after_prime == persisted_turn_id, (
        f"previous bugfix must restore _last_structured_turn_id to {persisted_turn_id!r}, got {cursor_after_prime!r}"
    )
    structured = [o for o in first_poll if isinstance(o, StructuredReplyOutput)]
    assert structured == [], (
        f"existing duplicate-turn guard must suppress the previously-acknowledged turn {persisted_turn_id!r}, got {structured!r}"
    )


# ---- Test 6: Cold start with only post-task turns --------------------------


@hypothesis.given(
    delta_post=_POST_TASK_DELTA_STRATEGY,
    turn_id=_PRESERVE_TURN_ID_STRATEGY,
)
@hypothesis.example(delta_post=timedelta(seconds=2), turn_id="keep-coldstart")
@hypothesis.settings(max_examples=15, deadline=None)
def test_preserve_cold_start_post_task_turn_emits_once(delta_post: timedelta, turn_id: str) -> None:
    """Cold start (no persisted cursor) + post-task turn -> emits exactly once.

    Validates: Requirements 3.4

    Models the genuine new-task scenario: persisted cursor is ``None``,
    snapshot exposes only a post-task turn ``B`` with ``B.ended_at >=
    task_started_at``. The presenter must emit ``B`` exactly once on the
    first poll, then suppress duplicates on subsequent polls.

    Sanity check that the new guard does NOT over-skip when the task
    context IS set but the turn is post-task.
    """
    ended_at = _FIXED_TASK_STARTED_AT + delta_post
    sessions = [
        _session(phase=SessionPhase.WAITING_FOR_INPUT),
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[_assistant_turn(turn_id, ended_at=ended_at)],
        ),
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[_assistant_turn(turn_id, ended_at=ended_at)],
        ),
    ]
    service = _PreservationTaskService(sessions)  # persisted_turn_id=None
    presenter = StructuredReplyPresenter(task_service=service, user_id=1, task_id="task-1")
    presenter._task_started_at = _FIXED_TASK_STARTED_AT  # type: ignore[attr-defined]

    asyncio.run(presenter.prime(baseline_current_snapshot=True))
    cursor_after_prime = presenter._last_structured_turn_id
    first_poll = asyncio.run(presenter.poll(task_id="task-1"))
    # Acknowledge the first emit so ``_last_structured_turn_id`` advances and
    # the duplicate-turn guard fires on the second poll (mirrors the
    # production pump).
    for output in first_poll:
        if isinstance(output, StructuredReplyOutput):
            asyncio.run(presenter.acknowledge_delivery(output))
    second_poll = asyncio.run(presenter.poll(task_id="task-1"))

    assert cursor_after_prime is None, f"cold-start prime should leave _last_structured_turn_id=None, got {cursor_after_prime!r}"
    structured_first = [o for o in first_poll if isinstance(o, StructuredReplyOutput)]
    assert len(structured_first) == 1, f"cold-start post-task turn must emit exactly once, got {structured_first!r}"
    assert structured_first[0].turn_id == turn_id
    structured_second = [o for o in second_poll if isinstance(o, StructuredReplyOutput)]
    assert structured_second == [], f"duplicate-turn guard must suppress the second emit, got {structured_second!r}"


# ---- Test 7: Non-baseline prime (baseline_current_snapshot=False) ----------


@hypothesis.given(
    persisted_turn_id=st.one_of(st.none(), _PERSISTED_TURN_ID_STRATEGY),
)
@hypothesis.example(persisted_turn_id=None)
@hypothesis.example(persisted_turn_id="persist-A")
@hypothesis.settings(max_examples=15, deadline=None)
def test_preserve_non_baseline_prime_uses_persisted_cursor(
    persisted_turn_id: str | None,
) -> None:
    """``baseline_current_snapshot=False`` -> ``_last_structured_turn_id == persisted``.

    Validates: Requirements 3.3

    The non-baseline prime path is unchanged: regardless of the snapshot or
    ``task_started_at``, the in-memory cursor is restored from the persisted
    cursor (or ``None`` if there is no persisted value). The new
    ``_collect_reply`` guard does not affect ``prime()`` at all.
    """
    sessions = [
        # Snapshot intentionally exposes a pre-task-shape turn so that any
        # accidental call into the new guard would change observable state;
        # we assert that ``prime()`` behavior is fully governed by the
        # persisted cursor regardless.
        _session(
            phase=SessionPhase.WAITING_FOR_INPUT,
            turns=[
                _assistant_turn(
                    "keep-snap-irrelevant",
                    ended_at=_FIXED_TASK_STARTED_AT - timedelta(hours=1),
                ),
            ],
        ),
    ]
    service = _PreservationTaskService(sessions, persisted_turn_id=persisted_turn_id)
    presenter = StructuredReplyPresenter(task_service=service, user_id=1, task_id="task-1")
    presenter._task_started_at = _FIXED_TASK_STARTED_AT  # type: ignore[attr-defined]

    asyncio.run(presenter.prime(baseline_current_snapshot=False))

    assert presenter._last_structured_turn_id == persisted_turn_id, (
        f"baseline=False must set _last_structured_turn_id to the persisted cursor: "
        f"expected {persisted_turn_id!r}, got {presenter._last_structured_turn_id!r}"
    )
