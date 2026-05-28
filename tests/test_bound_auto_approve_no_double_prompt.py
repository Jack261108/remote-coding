"""Regression: a bound external session with auto-approve active must NOT
also receive the permission button push notification.

Bug history: in the bound branch of _build_stage_list, both
`auto_approve_check` and `push_notification` stages were scheduled
unconditionally. When auto-approve was active, the user got both the silent
"🟢 Auto-approved" message AND the redundant "🔐 请求权限: ..." button
prompt.

Fix: `_run_auto_approve_check` raises `_StageShortCircuit` which terminates
the pipeline loop before the push_notification stage runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bootstrap_mixins import HookHandlingMixin, _StageShortCircuit
from app.domain.hook_models import HookEvent
from app.services.auto_approve_service import AutoApproveService


def _make_event(*, tool: str = "Edit", expects_response: bool = True) -> HookEvent:
    """Build a HookEvent. `expects_response` is a property derived from
    event=="PermissionRequest" and status=="waiting_for_approval"."""
    if expects_response:
        return HookEvent(
            session_id="sess-123",
            cwd="/Users/jack/project/remote-coding",
            event="PermissionRequest",
            status="waiting_for_approval",
            tool=tool,
            tool_input={"file_path": "/x.py"},
            tool_use_id="toolu_abc",
            pid=None,
        )
    return HookEvent(
        session_id="sess-123",
        cwd="/Users/jack/project/remote-coding",
        event="PostToolUse",
        status="processing",
        tool=tool,
        tool_input={"file_path": "/x.py"},
        tool_use_id="toolu_abc",
        pid=None,
    )


def _make_ownership(state: str = "bound", user_id: int = 42):
    """Create a minimal OwnershipResult-like object."""
    from enum import Enum

    class Origin(Enum):
        EXTERNAL = "external"

    return SimpleNamespace(
        ownership_state=state,
        origin=Origin.EXTERNAL,
        owner_user_id=user_id,
    )


class _Container(HookHandlingMixin):
    """Minimal container satisfying HookHandlingMixin's hasattr() checks."""

    def __init__(self) -> None:
        self.push_notifier = MagicMock()
        self.push_notifier.notify_permission_request = AsyncMock(return_value=True)
        self.push_notifier.notify_user_question = AsyncMock(return_value=True)
        self.push_notifier.notify_info = AsyncMock(return_value=True)
        self.push_notifier.notify_session_end = AsyncMock(return_value=True)

        self.auto_approve_service = AutoApproveService()

        self.hook_socket_server = SimpleNamespace(
            respond_to_permission=AsyncMock(return_value=True),
        )
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()

        self.claude_jsonl_parser = SimpleNamespace(
            extract_session_title=lambda session_id, cwd: None,
        )

    # Stubs for methods used by _build_stage_list's eagerly-constructed coroutines.
    async def _dispatch_session_event(self, event) -> None:
        pass

    def _schedule_jsonl_sync(self, session_id: str, cwd: str) -> None:
        pass

    async def _bind_hook_session(self, event) -> None:
        pass

    def _maybe_auto_file_send(self, event, owner_user_id) -> None:
        pass


async def _make_container(*, auto_approve_active: bool) -> _Container:
    container = _Container()
    if auto_approve_active:
        await container.auto_approve_service.activate("sess-123", user_id=42)
    return container


@pytest.mark.asyncio
async def test_auto_approve_check_raises_short_circuit() -> None:
    """_run_auto_approve_check must raise _StageShortCircuit when auto-approve
    is active, terminating the pipeline before push_notification runs."""
    container = await _make_container(auto_approve_active=True)
    event = _make_event(tool="Edit")

    with pytest.raises(_StageShortCircuit, match="auto-approved"):
        await container._run_auto_approve_check(event)


@pytest.mark.asyncio
async def test_auto_approve_check_does_not_short_circuit_when_inactive() -> None:
    """When auto-approve is NOT active, the stage must NOT raise."""
    container = await _make_container(auto_approve_active=False)
    event = _make_event(tool="Edit")

    # Should return normally (no exception)
    await container._run_auto_approve_check(event)


@pytest.mark.asyncio
async def test_auto_approve_check_does_not_short_circuit_for_ask_user_question() -> None:
    """AskUserQuestion is never auto-approved; the stage must not raise."""
    container = await _make_container(auto_approve_active=True)
    event = _make_event(tool="AskUserQuestion")

    await container._run_auto_approve_check(event)


@pytest.mark.asyncio
async def test_pipeline_bound_auto_approve_skips_push_notification() -> None:
    """Full pipeline integration: with auto-approve active on a bound session,
    the push_notification stage must NOT execute."""
    container = await _make_container(auto_approve_active=True)
    event = _make_event(tool="Edit")
    ownership = _make_ownership(state="bound", user_id=42)

    stages = container._build_stage_list(event, ownership)
    stage_names = [name for name, _ in stages]

    # Both stages are in the list (they are built unconditionally)
    assert "auto_approve_check" in stage_names
    assert "push_notification" in stage_names

    # Simulate pipeline execution
    executed_stages: list[str] = []
    short_circuited_at = -1
    for i, (stage_name, stage_coro) in enumerate(stages):
        try:
            await stage_coro
            executed_stages.append(stage_name)
        except _StageShortCircuit:
            executed_stages.append(f"{stage_name}:short-circuit")
            short_circuited_at = i
            break

    # Close unawaited coroutines (mimic production cleanup)
    for j in range(short_circuited_at + 1, len(stages)):
        coro = stages[j][1]
        if hasattr(coro, "close"):
            coro.close()

    # auto_approve_check must have short-circuited
    assert "auto_approve_check:short-circuit" in executed_stages
    # push_notification must NOT have been reached
    assert "push_notification" not in executed_stages
    # Permission was responded
    container.hook_socket_server.respond_to_permission.assert_awaited_once()
    # No redundant button prompt
    container.push_notifier.notify_permission_request.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_bound_no_auto_approve_sends_push_notification() -> None:
    """Without auto-approve, the push_notification stage runs normally and
    sends the permission-button prompt."""
    container = await _make_container(auto_approve_active=False)
    event = _make_event(tool="Edit")
    ownership = _make_ownership(state="bound", user_id=42)

    stages = container._build_stage_list(event, ownership)

    # Simulate pipeline execution
    executed_stages: list[str] = []
    for i, (stage_name, stage_coro) in enumerate(stages):
        try:
            await stage_coro
            executed_stages.append(stage_name)
        except _StageShortCircuit:
            executed_stages.append(f"{stage_name}:short-circuit")
            # Close remaining
            for j in range(i + 1, len(stages)):
                coro = stages[j][1]
                if hasattr(coro, "close"):
                    coro.close()
            break

    assert "push_notification" in executed_stages
    container.push_notifier.notify_permission_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_owned_auto_approve_short_circuits() -> None:
    """Owned sessions short-circuit on auto-approve, but session_binding,
    event_dispatch, and jsonl_sync still run before the short-circuit."""
    container = await _make_container(auto_approve_active=True)
    event = _make_event(tool="Edit")
    ownership = _make_ownership(state="owned", user_id=42)

    stages = container._build_stage_list(event, ownership)

    executed_stages: list[str] = []
    short_circuited_at = -1
    for i, (stage_name, stage_coro) in enumerate(stages):
        try:
            await stage_coro
            executed_stages.append(stage_name)
        except _StageShortCircuit:
            executed_stages.append(f"{stage_name}:short-circuit")
            short_circuited_at = i
            break

    # Close unawaited coroutines
    for j in range(short_circuited_at + 1, len(stages)):
        coro = stages[j][1]
        if hasattr(coro, "close"):
            coro.close()

    assert "auto_approve_check:short-circuit" in executed_stages
    # session_binding MUST have run before the short-circuit
    assert "session_binding" in executed_stages
    # auto_file_send must NOT have been reached (after auto_approve_check)
    assert "auto_file_send" not in executed_stages
