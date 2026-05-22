"""Property-based tests for external session list correctness.

Feature: external-session-takeover, Property 16: List correctness per user
"""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.external_session_models import ExternalBinding
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.external_binding_store import ExternalBindingStore
from app.domain.hook_models import HookEvent

import tempfile
from pathlib import Path

# --- Strategies ---

session_id_chars = st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
session_id_first_char = st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
session_ids = st.builds(
    lambda first, rest: first + rest,
    session_id_first_char,
    st.text(session_id_chars, min_size=1, max_size=30),
)

cwds = st.builds(
    lambda parts: "/" + "/".join(parts),
    st.lists(
        st.text(
            st.characters(whitelist_categories=("L", "N"), min_codepoint=65, max_codepoint=122),
            min_size=1,
            max_size=10,
        ),
        min_size=1,
        max_size=4,
    ),
)

user_ids = st.integers(min_value=1, max_value=10000)

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

pids = st.one_of(st.none(), st.integers(min_value=0, max_value=2**31))


def hook_events(session_id_strategy=session_ids):
    """Strategy that builds valid HookEvent instances."""
    return st.builds(
        HookEvent,
        session_id=session_id_strategy,
        cwd=cwds,
        event=hook_event_names,
        status=hook_statuses,
        pid=pids,
    )


# Strategy for generating a set of sessions, some unbound and some bound to different users
@st.composite
def session_mix(draw):
    """Generate a mix of unbound sessions and sessions bound to different users.

    Returns (unbound_session_ids, bindings_by_user) where bindings_by_user is
    a dict mapping user_id -> list of (session_id, cwd) tuples.
    """
    num_sessions = draw(st.integers(min_value=1, max_value=20))
    num_users = draw(st.integers(min_value=1, max_value=5))
    users = list(range(1, num_users + 1))

    # Generate unique session IDs
    generated_ids: set[str] = set()
    sessions: list[str] = []
    for _ in range(num_sessions):
        sid = draw(session_ids.filter(lambda s: s not in generated_ids))
        generated_ids.add(sid)
        sessions.append(sid)

    # Assign each session: either unbound or bound to a random user
    unbound_ids: list[tuple[str, str]] = []
    bindings_by_user: dict[int, list[tuple[str, str]]] = {uid: [] for uid in users}

    for sid in sessions:
        cwd = draw(cwds)
        is_bound = draw(st.booleans())
        if is_bound:
            uid = draw(st.sampled_from(users))
            bindings_by_user[uid].append((sid, cwd))
        else:
            unbound_ids.append((sid, cwd))

    return unbound_ids, bindings_by_user


class TestListCorrectnessPerUser:
    """Property 16: List correctness per user.

    **Validates: Requirements 6.1**

    Generate combinations of unbound sessions and sessions bound to different users.
    Verify list_unbound() returns all unbound, list_bound_for_user(uid) returns only
    that user's bindings. No sessions bound to other users should appear in a user's
    bound list.
    """

    @settings(max_examples=150)
    @given(data=session_mix())
    def test_list_unbound_returns_all_unbound_sessions(self, data: tuple):
        """list_unbound() returns exactly the set of unbound sessions."""
        unbound_ids, bindings_by_user = data

        discovery = ExternalSessionDiscoveryService()

        # Record unbound sessions via hook events
        for sid, cwd in unbound_ids:
            event = HookEvent(
                session_id=sid,
                cwd=cwd,
                event="UserPromptSubmit",
                status="running",
                pid=None,
            )
            discovery.record_event(event)

        # Verify list_unbound returns exactly the unbound sessions
        unbound_list = discovery.list_unbound()
        unbound_session_ids = {s.session_id for s in unbound_list}
        expected_unbound_ids = {sid for sid, _ in unbound_ids}

        assert unbound_session_ids == expected_unbound_ids

    @settings(max_examples=150)
    @given(data=session_mix())
    def test_list_bound_for_user_returns_only_that_users_bindings(self, data: tuple):
        """get_bindings_for_user(uid) returns only sessions bound to uid."""
        unbound_ids, bindings_by_user = data

        with tempfile.TemporaryDirectory() as tmp:
            store = ExternalBindingStore(data_dir=Path(tmp))

            # Save all bindings
            now = datetime.now(timezone.utc)
            for uid, sessions in bindings_by_user.items():
                for sid, cwd in sessions:
                    binding = ExternalBinding(
                        session_id=sid,
                        user_id=uid,
                        cwd=cwd,
                        bound_at=now,
                        jsonl_path=f"/tmp/{sid}.jsonl",
                    )
                    store.save_binding(binding)

            # Verify each user only sees their own bindings
            for uid, sessions in bindings_by_user.items():
                user_bindings = store.get_bindings_for_user(uid)
                user_binding_ids = {b.session_id for b in user_bindings}
                expected_ids = {sid for sid, _ in sessions}

                # User's bound list contains exactly their sessions
                assert user_binding_ids == expected_ids

                # No session bound to another user appears in this user's list
                for other_uid, other_sessions in bindings_by_user.items():
                    if other_uid == uid:
                        continue
                    other_ids = {sid for sid, _ in other_sessions}
                    assert user_binding_ids & other_ids == set()

    @settings(max_examples=150)
    @given(data=session_mix())
    def test_unbound_sessions_not_in_any_users_bound_list(self, data: tuple):
        """Sessions that are unbound do not appear in any user's bound list."""
        unbound_ids, bindings_by_user = data

        with tempfile.TemporaryDirectory() as tmp:
            store = ExternalBindingStore(data_dir=Path(tmp))

            # Save all bindings
            now = datetime.now(timezone.utc)
            for uid, sessions in bindings_by_user.items():
                for sid, cwd in sessions:
                    binding = ExternalBinding(
                        session_id=sid,
                        user_id=uid,
                        cwd=cwd,
                        bound_at=now,
                        jsonl_path=f"/tmp/{sid}.jsonl",
                    )
                    store.save_binding(binding)

            # Verify no unbound session appears in any user's bound list
            unbound_session_ids = {sid for sid, _ in unbound_ids}
            all_users = list(bindings_by_user.keys())

            for uid in all_users:
                user_bindings = store.get_bindings_for_user(uid)
                user_binding_ids = {b.session_id for b in user_bindings}
                assert user_binding_ids & unbound_session_ids == set()
