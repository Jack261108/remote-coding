"""Shared factories for external-session domain objects and session-service fakes.

Consolidates the per-file ``_make_binding`` / ``_make_hook_event`` /
``_make_context`` / ``_FakeSessionService`` copies that had drifted across the
external-session test files. Defaults mirror the most common variant; every
field can be overridden by keyword.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.external_session_models import ExternalBinding
from app.domain.hook_models import HookEvent
from app.domain.models import SessionContext, utc_now


def make_binding(
    *,
    session_id: str = "sess-1",
    user_id: int = 42,
    cwd: str = "/home/user/project",
    bound_at: datetime | None = None,
    jsonl_path: str | None = None,
    pid: int | None = None,
    tty: str | None = None,
    binding_id: str | None = None,
) -> ExternalBinding:
    """Build an ExternalBinding with common defaults; pass overrides as kwargs.

    ``binding_id=None`` keeps the dataclass's generated id; a string pins it.
    """
    binding = ExternalBinding(
        session_id=session_id,
        user_id=user_id,
        cwd=cwd,
        bound_at=bound_at if bound_at is not None else utc_now(),
        jsonl_path=jsonl_path,
        pid=pid,
        tty=tty,
    )
    if binding_id is not None:
        binding.binding_id = binding_id
    return binding


def make_hook_event(
    *,
    session_id: str = "s1",
    cwd: str = "/home/user/project",
    event: str = "PreToolUse",
    status: str = "running",
    pid: int | None = None,
    tty: str | None = None,
    tool: str | None = None,
    tool_input: dict | None = None,
    tool_use_id: str | None = None,
) -> HookEvent:
    """Build a valid HookEvent with common defaults; rest are opt-in."""
    return HookEvent(
        session_id=session_id,
        cwd=cwd,
        event=event,
        status=status,
        pid=pid,
        tty=tty,
        tool=tool,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
    )


def make_session_context(
    *,
    user_id: int = 1,
    claude_session_id: str | None = None,
    terminal_id: str | None = None,
    workdir: str = "/home/user/project",
) -> SessionContext:
    return SessionContext(
        user_id=user_id,
        session_id="internal-id",
        provider="claude_code",
        workdir=workdir,
        terminal_mode=terminal_id is not None,
        terminal_id=terminal_id,
        claude_session_id=claude_session_id,
    )


class FakeSessionService:
    """In-memory stand-in for SessionService's two reads the resolver uses.

    Mutate ``contexts`` directly between calls (replaces the old AsyncMock
    ``list_all.return_value = [...]`` injection style).
    """

    def __init__(self, contexts: list[SessionContext] | None = None) -> None:
        self.contexts = contexts if contexts is not None else []

    async def list_all(self) -> list[SessionContext]:
        return self.contexts

    async def lookup_by_claude_session_id(self, session_id: str) -> SessionContext | None:
        for ctx in self.contexts:
            if ctx.claude_session_id == session_id:
                return ctx
        return None
