from __future__ import annotations

import asyncio

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, run_state_machine_as_test

from app.services.permission_callback_registry import (
    AuthorizationMode,
    CallbackRecordStatus,
    ConsumeConsumed,
    InFlightConflictError,
    PermissionAction,
    PermissionCallbackRecordSnapshot,
    PermissionCallbackRegistry,
    SessionOrigin,
)

ALLOWED_TRANSITIONS = {
    CallbackRecordStatus.PENDING: {
        CallbackRecordStatus.PENDING,
        CallbackRecordStatus.CLAIMED,
        CallbackRecordStatus.SESSION_ENDED,
        CallbackRecordStatus.SUPERSEDED,
    },
    CallbackRecordStatus.CLAIMED: {
        CallbackRecordStatus.CLAIMED,
        CallbackRecordStatus.RESOLVED,
        CallbackRecordStatus.DISPATCH_FAILED,
        CallbackRecordStatus.SESSION_ENDED,
    },
    CallbackRecordStatus.RESOLVED: {CallbackRecordStatus.RESOLVED},
    CallbackRecordStatus.DISPATCH_FAILED: {
        CallbackRecordStatus.DISPATCH_FAILED,
        CallbackRecordStatus.SESSION_ENDED,
        CallbackRecordStatus.SUPERSEDED,
    },
    CallbackRecordStatus.SESSION_ENDED: {CallbackRecordStatus.SESSION_ENDED},
    CallbackRecordStatus.SUPERSEDED: {CallbackRecordStatus.SUPERSEDED},
}

APPEND_ONLY_FIELDS = ("decision", "responded_by_user_id", "responded_at", "dispatch_error_reason")


def _is_authorized(snapshot: PermissionCallbackRecordSnapshot, user_id: int) -> bool:
    if snapshot.authorization_mode is AuthorizationMode.ALL_USERS:
        return True
    if snapshot.authorization_mode is AuthorizationMode.SOLE_AUTO_APPROVE_USER:
        return snapshot.authorized_user_ids == frozenset({user_id})
    return user_id in snapshot.authorized_user_ids


class PermissionRegistryStateMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.now = 10_000.0
        self._token_counter = 0
        self.registry = PermissionCallbackRegistry(ttl_sec=600, token_factory=self._next_token, clock=lambda: self.now)
        self.loop = asyncio.new_event_loop()
        self._last_seen: dict[str, PermissionCallbackRecordSnapshot] = {}

    def teardown(self) -> None:
        self.loop.close()

    def _next_token(self) -> str:
        token = f"tk{self._token_counter:06d}"
        self._token_counter += 1
        return token

    def _call(self, awaitable):  # noqa: ANN001
        return self.loop.run_until_complete(awaitable)

    def _snapshot(self) -> dict[str, PermissionCallbackRecordSnapshot]:
        return {token: PermissionCallbackRecordSnapshot.from_record(record) for token, record in self.registry._records.items()}

    def _assert_invariants(self) -> None:
        current = self._snapshot()
        for token, previous in self._last_seen.items():
            current_snapshot = current.get(token)
            if current_snapshot is None:
                continue
            assert current_snapshot.status in ALLOWED_TRANSITIONS[previous.status]
            for field_name in APPEND_ONLY_FIELDS:
                previous_value = getattr(previous, field_name)
                if previous_value is not None:
                    assert getattr(current_snapshot, field_name) == previous_value
        self._last_seen = current

    def _select_token(self, token_index: int) -> str:
        tokens = sorted(self.registry._records)
        if not tokens:
            return "missing0"
        return tokens[token_index % len(tokens)]

    @rule(
        session_index=st.integers(min_value=0, max_value=2),
        tool_index=st.integers(min_value=0, max_value=2),
        origin=st.sampled_from(list(SessionOrigin)),
        authorization_mode=st.sampled_from(list(AuthorizationMode)),
    )
    def register_token(self, session_index: int, tool_index: int, origin: SessionOrigin, authorization_mode: AuthorizationMode) -> None:
        session_id = f"session-{session_index}"
        tool_use_id = f"tool-{tool_index}"
        compound_key = (session_id, tool_use_id)
        existing_token = self.registry._compound_index.get(compound_key)
        existing_record = self.registry._records.get(existing_token) if existing_token is not None else None
        before_records = repr(self.registry._records)
        before_index = repr(self.registry._compound_index)
        authorized_user_ids = frozenset({1}) if authorization_mode is AuthorizationMode.SOLE_AUTO_APPROVE_USER else frozenset({1, 2})

        if existing_record is not None and existing_record.status is CallbackRecordStatus.CLAIMED:
            with pytest.raises(InFlightConflictError):
                self._call(
                    self.registry.register_token(
                        tool_use_id=tool_use_id,
                        session_id=session_id,
                        origin=origin,
                        authorization_mode=authorization_mode,
                        authorized_user_ids=authorized_user_ids,
                    )
                )
            assert repr(self.registry._records) == before_records
            assert repr(self.registry._compound_index) == before_index
        else:
            token = self._call(
                self.registry.register_token(
                    tool_use_id=tool_use_id,
                    session_id=session_id,
                    origin=origin,
                    authorization_mode=authorization_mode,
                    authorized_user_ids=authorized_user_ids,
                )
            )
            assert token in self.registry._records
            assert self.registry._compound_index[compound_key] == token

        self._assert_invariants()

    @rule(
        token_index=st.integers(min_value=0, max_value=20),
        user_id=st.integers(min_value=1, max_value=4),
        action=st.sampled_from(list(PermissionAction)),
    )
    def consume(self, token_index: int, user_id: int, action: PermissionAction) -> None:
        token = self._select_token(token_index)
        before = self._snapshot().get(token)
        result = self._call(self.registry.consume(token, user_id, action))

        if before is not None and before.status is CallbackRecordStatus.PENDING and _is_authorized(before, user_id):
            assert isinstance(result, ConsumeConsumed)
            assert self.registry._records[token].status is CallbackRecordStatus.CLAIMED
            assert self.registry._records[token].decision is action
            assert self.registry._records[token].responded_by_user_id == user_id

        self._assert_invariants()

    @rule(token_index=st.integers(min_value=0, max_value=20))
    def mark_resolved(self, token_index: int) -> None:
        token = self._select_token(token_index)
        before = self._snapshot().get(token)
        before_records = repr(self.registry._records)
        before_index = repr(self.registry._compound_index)

        result = self._call(self.registry.mark_resolved(token))

        if before is not None and before.status is CallbackRecordStatus.CLAIMED:
            assert result is True
            assert self.registry._records[token].status is CallbackRecordStatus.RESOLVED
        else:
            assert result is False
            assert repr(self.registry._records) == before_records
            assert repr(self.registry._compound_index) == before_index

        self._assert_invariants()

    @rule(token_index=st.integers(min_value=0, max_value=20), reason=st.sampled_from(["backend_down", "dispatch_unknown", "timeout"]))
    def mark_dispatch_failed(self, token_index: int, reason: str) -> None:
        token = self._select_token(token_index)
        before = self._snapshot().get(token)
        before_records = repr(self.registry._records)
        before_index = repr(self.registry._compound_index)

        result = self._call(self.registry.mark_dispatch_failed(token, reason))

        if before is not None and before.status is CallbackRecordStatus.CLAIMED:
            assert result is True
            assert self.registry._records[token].status is CallbackRecordStatus.DISPATCH_FAILED
            assert self.registry._records[token].dispatch_error_reason == reason
        else:
            assert result is False
            assert repr(self.registry._records) == before_records
            assert repr(self.registry._compound_index) == before_index

        self._assert_invariants()

    @rule(session_index=st.integers(min_value=0, max_value=2))
    def invalidate_session(self, session_index: int) -> None:
        session_id = f"session-{session_index}"
        before = self._snapshot()
        expected_count = sum(
            1
            for snapshot in before.values()
            if snapshot.session_id == session_id
            and snapshot.status in {CallbackRecordStatus.PENDING, CallbackRecordStatus.CLAIMED, CallbackRecordStatus.DISPATCH_FAILED}
        )

        result = self._call(self.registry.invalidate_session(session_id))

        assert result == expected_count
        for token, record in self.registry._records.items():
            if record.session_id == session_id and before[token].status in {
                CallbackRecordStatus.PENDING,
                CallbackRecordStatus.CLAIMED,
                CallbackRecordStatus.DISPATCH_FAILED,
            }:
                assert record.status is CallbackRecordStatus.SESSION_ENDED
        for token in self.registry._compound_index.values():
            record = self.registry._records[token]
            assert not (record.session_id == session_id and record.status is CallbackRecordStatus.SESSION_ENDED)

        self._assert_invariants()


def test_permission_registry_state_machine() -> None:
    run_state_machine_as_test(PermissionRegistryStateMachine, settings=settings(max_examples=40, stateful_step_count=25, deadline=None))
