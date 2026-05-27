from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.permission_callback_registry import (
    AuthorizationMode,
    CallbackRecordStatus,
    ConsumeAlreadyResponded,
    ConsumeConsumed,
    PermissionAction,
    PermissionCallbackRegistry,
    SessionOrigin,
)

BASE_SECONDS = 3_000.0


@settings(max_examples=50, deadline=None)
@given(
    responders=st.lists(st.integers(min_value=1, max_value=10_000), min_size=2, max_size=8, unique=True),
    actions=st.lists(st.sampled_from(list(PermissionAction)), min_size=2, max_size=8),
)
def test_concurrent_consume_is_first_responder_wins(responders: list[int], actions: list[PermissionAction]) -> None:
    actions = [actions[index % len(actions)] for index in range(len(responders))]

    async def run_scenario() -> None:
        registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "tok00001", clock=lambda: BASE_SECONDS)
        token = await registry.register_token(
            tool_use_id="tool-1",
            session_id="session-1",
            origin=SessionOrigin.EXTERNAL_UNBOUND,
            authorization_mode=AuthorizationMode.ALLOWED_USERS_SNAPSHOT,
            authorized_user_ids=frozenset(responders),
        )
        gate = asyncio.Event()

        async def consume(user_id: int, action: PermissionAction):
            await gate.wait()
            return await registry.consume(token, user_id, action)

        tasks = [asyncio.create_task(consume(user_id, action)) for user_id, action in zip(responders, actions, strict=True)]
        gate.set()
        results = await asyncio.gather(*tasks)

        consumed = [result for result in results if isinstance(result, ConsumeConsumed)]
        already_responded = [result for result in results if isinstance(result, ConsumeAlreadyResponded)]

        assert len(consumed) == 1
        assert len(already_responded) == len(responders) - 1
        winner_snapshot = consumed[0].snapshot
        record = registry._records[token]
        assert record.status is CallbackRecordStatus.CLAIMED
        assert record.decision is winner_snapshot.decision
        assert record.responded_by_user_id == winner_snapshot.responded_by_user_id
        assert record.responded_at == winner_snapshot.responded_at
        assert winner_snapshot.responded_by_user_id in responders

    asyncio.run(run_scenario())
