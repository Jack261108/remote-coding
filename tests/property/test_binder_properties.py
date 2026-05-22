"""Property-based tests for ExternalSessionBinder.

Feature: external-session-takeover
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.hook_models import HookEvent
from app.services.external_binding_store import ExternalBindingStore
from app.services.external_session_binder import ExternalSessionBinder, _resolve_jsonl_path
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
        st.text(
            st.characters(whitelist_categories=("L", "N"), min_codepoint=65, max_codepoint=122),
            min_size=1,
            max_size=10,
        ),
        min_size=1,
        max_size=4,
    ),
)

# pid: optional non-negative int
pids = st.one_of(st.none(), st.integers(min_value=0, max_value=2**31))

hook_event_names = st.sampled_from(
    [
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SessionStart",
    ]
)

hook_statuses = st.sampled_from(
    [
        "starting",
        "processing",
        "running",
        "ended",
    ]
)

user_ids = st.integers(min_value=1, max_value=2**31)


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


def _make_binder(tmp_path: Path):
    """Create a fresh binder with temp directories."""
    binding_store_dir = tmp_path / "bindings"
    binding_store_dir.mkdir(parents=True, exist_ok=True)
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    discovery = ExternalSessionDiscoveryService()
    store = ExternalBindingStore(data_dir=binding_store_dir)
    binder = ExternalSessionBinder(
        discovery=discovery,
        binding_store=store,
        projects_dir=projects_dir,
    )
    return discovery, store, binder, projects_dir


# --- Property 3: Successful bind associates user and removes from discoverable ---


class TestSuccessfulBind:
    """Property 3: Successful bind associates user and removes from discoverable.

    **Validates: Requirements 2.1, 2.2, 3.1**

    For any valid user_id and session that exists in the discovery list,
    bind succeeds, removes the session from unbound, stores the binding,
    and resolves the JSONL path.
    """

    @settings(max_examples=100)
    @given(event=hook_events(), user_id=user_ids)
    def test_bind_succeeds_and_removes_from_unbound(self, event: HookEvent, user_id: int):
        """Binding a discoverable session succeeds and removes it from unbound list."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            discovery, store, binder, projects_dir = _make_binder(tmp_path)

            # Record event to make session discoverable
            discovery.record_event(event)
            assert discovery.get(event.session_id) is not None

            # Bind
            result = asyncio.get_event_loop().run_until_complete(binder.bind(user_id=user_id, session_id=event.session_id))

            # Verify bind succeeded
            assert result.success is True
            assert result.session_id == event.session_id
            assert result.jsonl_path is not None

            # Session removed from discovery
            assert discovery.get(event.session_id) is None
            unbound_ids = {s.session_id for s in discovery.list_unbound()}
            assert event.session_id not in unbound_ids

            # Binding exists in store
            binding = store.get_binding(event.session_id)
            assert binding is not None
            assert binding.user_id == user_id
            assert binding.session_id == event.session_id
            assert binding.cwd == event.cwd

            # JSONL path is resolved correctly
            expected_path = _resolve_jsonl_path(
                session_id=event.session_id,
                cwd=event.cwd,
                projects_dir=projects_dir,
            )
            assert result.jsonl_path == expected_path


# --- Property 4: Bind rejects invalid requests ---


class TestBindRejectsInvalid:
    """Property 4: Bind rejects invalid requests.

    **Validates: Requirements 2.3, 2.4**

    Bind attempts for non-existent sessions or already-bound sessions
    fail without modifying system state.
    """

    @settings(max_examples=100)
    @given(session_id=session_ids, user_id=user_ids)
    def test_bind_non_existent_session_fails(self, session_id: str, user_id: int):
        """Binding a session not in discovery fails."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            discovery, store, binder, _ = _make_binder(tmp_path)

            # Don't record any events - session not discoverable
            result = asyncio.get_event_loop().run_until_complete(binder.bind(user_id=user_id, session_id=session_id))

            assert result.success is False
            # State unchanged
            assert discovery.list_unbound() == []
            assert store.get_binding(session_id) is None

    @settings(max_examples=100)
    @given(event=hook_events(), user_id=user_ids, second_user_id=user_ids)
    def test_bind_already_bound_session_fails(self, event: HookEvent, user_id: int, second_user_id: int):
        """Binding an already-bound session fails and state is unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            discovery, store, binder, _ = _make_binder(tmp_path)

            # Record and bind
            discovery.record_event(event)
            asyncio.get_event_loop().run_until_complete(binder.bind(user_id=user_id, session_id=event.session_id))

            # Capture state after first bind
            binding_after_first = store.get_binding(event.session_id)
            assert binding_after_first is not None

            # Try to bind again (session no longer in discovery)
            result = asyncio.get_event_loop().run_until_complete(binder.bind(user_id=second_user_id, session_id=event.session_id))

            assert result.success is False

            # Binding unchanged - still belongs to first user
            binding_after_second = store.get_binding(event.session_id)
            assert binding_after_second is not None
            assert binding_after_second.user_id == user_id


# --- Property 5: Unbind round-trip restores discoverability ---


class TestUnbindRoundTrip:
    """Property 5: Unbind round-trip restores discoverability.

    **Validates: Requirements 5.3**

    After binding and then unbinding a session, the binding is removed
    from the store. The session will be re-discovered on the next hook
    event (not immediately added back to discovery by design).
    """

    @settings(max_examples=100)
    @given(event=hook_events(), user_id=user_ids)
    def test_unbind_removes_binding_from_store(self, event: HookEvent, user_id: int):
        """Unbinding removes the binding from the store."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            discovery, store, binder, _ = _make_binder(tmp_path)

            # Record, bind, then unbind
            discovery.record_event(event)
            bind_result = asyncio.get_event_loop().run_until_complete(binder.bind(user_id=user_id, session_id=event.session_id))
            assert bind_result.success is True

            unbind_result = asyncio.get_event_loop().run_until_complete(binder.unbind(user_id=user_id, session_id=event.session_id))

            # Unbind succeeds
            assert unbind_result.success is True
            assert unbind_result.session_id == event.session_id

            # Binding removed from store
            assert store.get_binding(event.session_id) is None

            # Session is NOT immediately back in discovery (by design)
            # It will be re-discovered on next hook event
            assert discovery.get(event.session_id) is None


# --- Property 6: JSONL path resolution ---


class TestJsonlPathResolution:
    """Property 6: JSONL path resolution.

    **Validates: Requirements 3.1**

    For any valid session_id and cwd, the resolved JSONL path matches
    the convention: projects_dir / <sanitized_cwd> / <session_id>.jsonl
    where sanitized_cwd replaces '/' with '-' and '.' with '-'.
    """

    @settings(max_examples=100)
    @given(session_id=session_ids, cwd=cwds)
    def test_jsonl_path_matches_convention(self, session_id: str, cwd: str):
        """Computed path matches projects_dir / sanitized_cwd / session_id.jsonl."""
        with tempfile.TemporaryDirectory() as tmp:
            projects_dir = Path(tmp) / "projects"
            projects_dir.mkdir()

            result = _resolve_jsonl_path(
                session_id=session_id,
                cwd=cwd,
                projects_dir=projects_dir,
            )

            # Verify structure
            sanitized_cwd = cwd.replace("/", "-").replace(".", "-")
            expected = projects_dir / sanitized_cwd / f"{session_id}.jsonl"
            assert result == expected

            # Verify it's under projects_dir
            assert str(result).startswith(str(projects_dir))

            # Verify filename
            assert result.name == f"{session_id}.jsonl"

            # Verify parent dir name is sanitized cwd
            assert result.parent.name == sanitized_cwd
