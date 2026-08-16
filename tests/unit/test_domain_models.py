from __future__ import annotations

from app.domain.models import FINAL_STATUSES, TERMINAL_EVENT_TYPES, EventType, TaskStatus


def test_terminal_event_types_contains_all_four_terminal_events() -> None:
    assert TERMINAL_EVENT_TYPES == {
        EventType.EXITED,
        EventType.FAILED,
        EventType.TIMEOUT,
        EventType.CANCELED,
    }


def test_exited_is_terminal_event_type() -> None:
    assert EventType.EXITED in TERMINAL_EVENT_TYPES


def test_started_is_not_terminal_event_type() -> None:
    assert EventType.STARTED not in TERMINAL_EVENT_TYPES
    assert EventType.STDOUT not in TERMINAL_EVENT_TYPES
    assert EventType.STDERR not in TERMINAL_EVENT_TYPES


def test_terminal_event_types_distinct_from_final_statuses() -> None:
    # EXITED event corresponds to successful completion (SUCCEEDED status),
    # so TERMINAL_EVENT_TYPES and FINAL_STATUSES live at different layers.
    assert TERMINAL_EVENT_TYPES.issubset({e for e in EventType})
    assert FINAL_STATUSES == {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.TIMEOUT,
        TaskStatus.CANCELED,
    }
