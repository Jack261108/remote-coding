from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.user_question_models import (
    ExternalGhosttyQuestionTarget,
    ExternalTmuxQuestionTarget,
    ExternalUserQuestionPhase,
    UserQuestionPrompt,
)
from app.services.external_user_question_state import (
    ExternalQuestionPendingAmbiguous,
    ExternalQuestionPendingNone,
    ExternalQuestionPendingUnique,
    ExternalUserQuestionState,
    PendingExternalUserQuestion,
)


def _prompt(tool_use_id: str) -> tuple[UserQuestionPrompt, ...]:
    return (
        UserQuestionPrompt(
            tool_use_id=tool_use_id,
            question_index=0,
            total_questions=1,
            question="Choose",
        ),
    )


def _ghostty_target(*, paired_at: datetime | None = None) -> ExternalGhosttyQuestionTarget:
    return ExternalGhosttyQuestionTarget(
        binding_id="binding-1",
        terminal_id="terminal-1",
        paired_tty="/dev/ttys005",
        paired_at=paired_at or datetime.now(UTC),
    )


def _pending(
    tool_use_id: str,
    *,
    user_id: int = 42,
    session_id: str = "session-1",
    target=None,
) -> PendingExternalUserQuestion:
    return PendingExternalUserQuestion(
        tool_use_id=tool_use_id,
        session_id=session_id,
        user_id=user_id,
        prompts=_prompt(tool_use_id),
        target=target or _ghostty_target(),
    )


def test_store_returns_immutable_snapshot_for_tmux_and_ghostty() -> None:
    state = ExternalUserQuestionState()
    state.store(_pending("tool-ghostty"))
    state.store(
        _pending(
            "tool-tmux",
            target=ExternalTmuxQuestionTarget(pane_id="%1", tmux_bin="tmux-custom"),
        )
    )

    ghostty = state.get("tool-ghostty")
    tmux = state.get("tool-tmux")

    assert ghostty is not None and ghostty.target.kind == "ghostty"
    assert tmux is not None and tmux.target == ExternalTmuxQuestionTarget(pane_id="%1", tmux_bin="tmux-custom")


def test_resolve_unique_active_for_user_is_fail_closed_on_ambiguity() -> None:
    state = ExternalUserQuestionState()
    assert isinstance(state.resolve_unique_active_for_user(42), ExternalQuestionPendingNone)

    state.store(_pending("tool-1"))
    unique = state.resolve_unique_active_for_user(42)
    assert isinstance(unique, ExternalQuestionPendingUnique)
    assert unique.pending.tool_use_id == "tool-1"

    state.store(_pending("tool-2", session_id="session-2"))
    ambiguous = state.resolve_unique_active_for_user(42)
    assert isinstance(ambiguous, ExternalQuestionPendingAmbiguous)
    assert ambiguous.count == 2


def test_resolve_unique_active_filters_by_kind_across_mixed_targets() -> None:
    """A user with one ghostty and one tmux pending is not ambiguous per-kind.

    ``resolve_unique_active_for_user(kind="ghostty")`` must return the single
    ghostty question (not an ambiguous count of 2), so the free-text router —
    which always queries ``kind="ghostty"`` — cannot be tripped by a concurrent
    external tmux question on the same user.
    """
    state = ExternalUserQuestionState()
    state.store(_pending("tool-ghostty"))

    tmux_target = ExternalTmuxQuestionTarget(pane_id="%1", tmux_bin="tmux")
    state.store(
        PendingExternalUserQuestion(
            tool_use_id="tool-tmux",
            session_id="session-2",
            user_id=42,
            prompts=_prompt("tool-tmux"),
            target=tmux_target,
        )
    )

    ghostty = state.resolve_unique_active_for_user(42, kind="ghostty")
    assert isinstance(ghostty, ExternalQuestionPendingUnique)
    assert ghostty.pending.tool_use_id == "tool-ghostty"

    tmux = state.resolve_unique_active_for_user(42, kind="tmux")
    assert isinstance(tmux, ExternalQuestionPendingUnique)
    assert tmux.pending.tool_use_id == "tool-tmux"


def test_phase_transitions_require_expected_target_and_order() -> None:
    state = ExternalUserQuestionState()
    target = _ghostty_target()
    state.store(_pending("tool-1", target=target))

    assert not state.mark_completed(tool_use_id="tool-1", expected_target=target)
    assert not state.mark_terminal_action_applied(
        tool_use_id="tool-1",
        expected_target=_ghostty_target(paired_at=target.paired_at + timedelta(seconds=1)),
    )
    assert state.mark_terminal_action_applied(tool_use_id="tool-1", expected_target=target)
    assert state.get_active("tool-1") is None
    assert state.mark_completed(tool_use_id="tool-1", expected_target=target)
    assert state.get("tool-1").phase is ExternalUserQuestionPhase.COMPLETED  # type: ignore[union-attr]


def test_mark_indeterminate_blocks_active_question() -> None:
    state = ExternalUserQuestionState()
    target = _ghostty_target()
    state.store(_pending("tool-1", target=target))

    assert state.mark_indeterminate(tool_use_id="tool-1", expected_target=target, reason="timeout")
    snapshot = state.get("tool-1")
    assert snapshot is not None
    assert snapshot.phase is ExternalUserQuestionPhase.INDETERMINATE
    assert snapshot.failure_reason == "timeout"
    assert state.get_active("tool-1") is None


def test_mark_indeterminate_from_terminal_action_applied() -> None:
    """``TERMINAL_ACTION_APPLIED`` may still collapse to ``INDETERMINATE``.

    The final-question path pushes the record to ``TERMINAL_ACTION_APPLIED``
    (transport ``submit_after=True``) before the Hook ``allow``; if that allow
    fails (``write_failed``), ``question_indeterminate`` must still be able to
    mark the record. This pairs with ``test_mark_indeterminate_blocks_active_question``
    and covers the transition at the state-API layer, independent of transport.
    """
    state = ExternalUserQuestionState()
    target = _ghostty_target()
    state.store(_pending("tool-1", target=target))
    assert state.mark_terminal_action_applied(tool_use_id="tool-1", expected_target=target)

    marked = state.mark_indeterminate(tool_use_id="tool-1", expected_target=target, reason="hook_allow_failed")

    assert marked is True
    snapshot = state.get("tool-1")
    assert snapshot is not None
    assert snapshot.phase is ExternalUserQuestionPhase.INDETERMINATE
    assert snapshot.failure_reason == "hook_allow_failed"
    # Once indeterminate the record is no longer active, and cannot transition
    # forward to COMPLETED (the tower)
    assert state.get_active("tool-1") is None
    assert not state.mark_completed(tool_use_id="tool-1", expected_target=target)
    assert snapshot.phase is ExternalUserQuestionPhase.INDETERMINATE


def test_repair_with_same_terminal_and_tty_invalidates_old_paired_at() -> None:
    state = ExternalUserQuestionState()
    paired_at = datetime.now(UTC)
    target = _ghostty_target(paired_at=paired_at)
    state.store(_pending("tool-1", target=target))

    assert (
        state.invalidate_ghostty_target(
            session_id="session-1",
            binding_id="binding-1",
            terminal_id="terminal-1",
            paired_tty="/dev/ttys005",
            paired_at=paired_at + timedelta(seconds=1),
        )
        == 0
    )
    assert state.get("tool-1") is not None
    assert (
        state.invalidate_ghostty_target(
            session_id="session-1",
            binding_id="binding-1",
            terminal_id="terminal-1",
            paired_tty="/dev/ttys005",
            paired_at=paired_at,
        )
        == 1
    )
    assert state.get("tool-1") is None


def test_ttl_prunes_active_but_keeps_recent_terminal_tombstone() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    current = now
    state = ExternalUserQuestionState(ttl_sec=10, wall_clock=lambda: current)
    target = _ghostty_target(paired_at=now)
    state.store(_pending("active", target=target))
    state.store(_pending("done", target=target))
    assert state.mark_terminal_action_applied(tool_use_id="done", expected_target=target)
    assert state.mark_completed(tool_use_id="done", expected_target=target)

    current = now + timedelta(seconds=11)
    removed = state.prune_stale()

    assert [item.tool_use_id for item in removed] == ["active"]
    assert state.get("done") is not None


def test_remove_if_matches_does_not_remove_new_target_generation() -> None:
    state = ExternalUserQuestionState()
    old = _ghostty_target()
    new = _ghostty_target(paired_at=old.paired_at + timedelta(seconds=1))
    state.store(_pending("tool-1", target=new))

    assert state.remove_if_matches(tool_use_id="tool-1", session_id="session-1", expected_target=old) is None
    assert state.get("tool-1") is not None
    assert state.remove_if_matches(tool_use_id="tool-1", session_id="session-1", expected_target=new) is not None
