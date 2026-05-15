from types import SimpleNamespace

from app.domain.session_models import ConversationTurn, PendingPermission, SessionPhase, ToolCallRecord


def make_structured_session(
    *,
    phase: SessionPhase,
    turns: list[ConversationTurn] | None = None,
    pending: PendingPermission | None = None,
    tool_calls: dict[str, ToolCallRecord] | None = None,
    session_id: str = "claude-session-1",
):
    return SimpleNamespace(
        session_id=session_id,
        phase=phase,
        turns=turns or [],
        pending_permission=pending,
        tool_calls=tool_calls or {},
    )
