"""Property-based tests for ExternalSessionDiscoveryService.

Feature: external-session-takeover
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.hook_models import HookEvent
from app.services.external_session_discovery import ExternalSessionDiscoveryService

# --- Strategies ---

# session_id must match ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$
session_id_chars = st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
session_id_first_char = st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
session_ids = st.builds(
    lambda first, rest: first + rest,
    session_id_first_char,
    st.text(session_id_chars, min_size=0, max_size=30),
)

# cwd must be an absolute path
cwds = st.builds(
    lambda parts: "/" + "/".join(parts),
    st.lists(
        st.text(st.characters(whitelist_categories=("L", "N"), min_codepoint=65, max_codepoint=122), min_size=1, max_size=10),
        min_size=1,
        max_size=4,
    ),
)

# pid: optional non-negative int
pids = st.one_of(st.none(), st.integers(min_value=0, max_value=2**31))

# valid hook event names
hook_event_names = st.sampled_from(
    [
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PermissionRequest",
        "Notification",
        "Stop",
        "SubagentStop",
        "SessionStart",
        "SessionEnd",
        "PreCompact",
        "PostToolUseFailure",
        "SubagentStart",
        "PostCompact",
        "StopFailure",
        "PermissionDenied",
    ]
)

hook_statuses = st.sampled_from(
    [
        "starting",
        "processing",
        "running",
        "running_tool",
        "waiting_for_approval",
        "waiting_for_input",
        "ended",
        "failed",
    ]
)


def hook_events(
    session_id_strategy=session_ids,
    event_strategy=hook_event_names,
):
    """Strategy that builds valid HookEvent instances."""
    return st.builds(
        HookEvent,
        session_id=session_id_strategy,
        cwd=cwds,
        event=event_strategy,
        status=hook_statuses,
        pid=pids,
    )


# --- Property 1: Unbound event recording ---


class TestUnboundEventRecording:
    """Property 1: Unbound event recording.

    **Validates: Requirements 1.1, 1.2, 1.4**

    For any sequence of hook events recorded into the discovery service,
    each unique session_id appears in the unbound list with correct metadata.
    """

    @settings(max_examples=150)
    @given(events=st.lists(hook_events(), min_size=1, max_size=20))
    def test_recorded_events_appear_in_unbound_list(self, events: list[HookEvent]):
        """Every recorded session_id is discoverable in unbound list."""
        svc = ExternalSessionDiscoveryService()
        for ev in events:
            svc.record_event(ev)

        unbound = svc.list_unbound()
        unbound_ids = {s.session_id for s in unbound}

        # Every unique session_id from events must be in unbound list
        expected_ids = {ev.session_id for ev in events}
        assert unbound_ids == expected_ids

    @settings(max_examples=150)
    @given(events=st.lists(hook_events(), min_size=1, max_size=20))
    def test_metadata_correctness(self, events: list[HookEvent]):
        """Unbound sessions have correct session_id, cwd, pid, and first_seen set."""
        svc = ExternalSessionDiscoveryService()
        for ev in events:
            svc.record_event(ev)

        for session in svc.list_unbound():
            assert session.session_id != ""
            assert session.cwd.startswith("/")
            assert session.first_seen is not None
            assert isinstance(session.first_seen, datetime)
            assert session.first_seen.tzinfo is not None
            # pid is either None or non-negative int
            assert session.pid is None or session.pid >= 0


# --- Property 2: SessionEnd removes from tracking ---


class TestSessionEndRemoval:
    """Property 2: SessionEnd removes from tracking.

    **Validates: Requirements 1.3, 5.1**

    After recording events for sessions and then removing them,
    those sessions no longer appear in the discoverable list.
    """

    @settings(max_examples=150)
    @given(
        events=st.lists(hook_events(), min_size=1, max_size=15),
        remove_fraction=st.floats(min_value=0.0, max_value=1.0),
    )
    def test_remove_session_removes_from_list(self, events: list[HookEvent], remove_fraction: float):
        """Sessions removed via remove_session are no longer discoverable."""
        svc = ExternalSessionDiscoveryService()
        for ev in events:
            svc.record_event(ev)

        all_ids = list({ev.session_id for ev in events})
        # Remove a fraction of sessions
        num_to_remove = int(len(all_ids) * remove_fraction)
        to_remove = set(all_ids[:num_to_remove])

        for sid in to_remove:
            svc.remove_session(sid)

        remaining = {s.session_id for s in svc.list_unbound()}
        # Removed sessions must not appear
        assert remaining & to_remove == set()
        # Non-removed sessions must still appear
        assert remaining == set(all_ids) - to_remove


# --- Property 7: Stale session pruning ---


class TestStaleSessionPruning:
    """Property 7: Stale session pruning.

    **Validates: Requirements 5.4**

    prune_stale removes exactly those sessions whose last_seen exceeds
    stale_timeout_sec, and retains all others.
    """

    @settings(max_examples=150)
    @given(
        data=st.data(),
        num_sessions=st.integers(min_value=1, max_value=15),
        stale_timeout=st.floats(min_value=1.0, max_value=3600.0),
    )
    def test_prune_removes_exactly_stale_sessions(self, data, num_sessions: int, stale_timeout: float):
        """Only sessions with last_seen > stale_timeout_sec are pruned."""
        svc = ExternalSessionDiscoveryService(stale_timeout_sec=stale_timeout)

        # Generate unique session events and record them
        generated_ids: list[str] = []
        for _ in range(num_sessions):
            ev = data.draw(hook_events())
            # Ensure unique session_ids
            while ev.session_id in generated_ids:
                ev = data.draw(hook_events())
            generated_ids.append(ev.session_id)
            svc.record_event(ev)

        # Now manipulate last_seen to create a mix of stale and fresh sessions
        now = datetime.now(timezone.utc)
        expected_stale: set[str] = set()
        expected_fresh: set[str] = set()

        for sid in generated_ids:
            session = svc.get(sid)
            assert session is not None
            # Draw whether this session should be stale
            is_stale = data.draw(st.booleans())
            if is_stale:
                # Set last_seen far in the past (beyond timeout)
                offset = data.draw(st.floats(min_value=stale_timeout + 1, max_value=stale_timeout + 7200))
                session.last_seen = now - timedelta(seconds=offset)
                expected_stale.add(sid)
            else:
                # Set last_seen recently (within timeout)
                offset = data.draw(st.floats(min_value=0.0, max_value=max(stale_timeout - 1, 0.0)))
                session.last_seen = now - timedelta(seconds=offset)
                expected_fresh.add(sid)

        # Run prune
        pruned_ids = set(svc.prune_stale())

        # Pruned set should be exactly the stale sessions
        assert pruned_ids == expected_stale

        # Remaining should be exactly the fresh sessions
        remaining_ids = {s.session_id for s in svc.list_unbound()}
        assert remaining_ids == expected_fresh
