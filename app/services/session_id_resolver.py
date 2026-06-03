from __future__ import annotations

from dataclasses import dataclass

from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService


def _resolve_session_id(
    session_id_prefix: str,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> tuple[str | None, str | None]:
    """Resolve a partial session_id prefix to a full session_id.

    Searches both unbound discovery list and bound sessions.
    Returns (full_session_id, error_message). If ambiguous, returns error.
    """
    prefix = session_id_prefix.rstrip(".")
    candidates: list[str] = []

    for s in discovery.list_unbound():
        if s.session_id == prefix or s.session_id.startswith(prefix):
            candidates.append(s.session_id)

    for b in binder._binding_store.load_all().values():
        if b.session_id == prefix or b.session_id.startswith(prefix):
            if b.session_id not in candidates:
                candidates.append(b.session_id)

    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) == 0:
        return None, "Session not found"
    return None, f"Ambiguous prefix, {len(candidates)} matches. Be more specific."


@dataclass(frozen=True, slots=True)
class BindResult:
    success: bool
    session_id: str | None = None
    message: str = ""
    conversation_available: bool = False


async def resolve_and_bind(
    session_id_prefix: str,
    *,
    user_id: int,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> BindResult:
    """Resolve a session ID prefix and bind the session to a user.

    Returns a BindResult with the outcome. Shared by command and callback handlers.
    """
    resolved, error = _resolve_session_id(session_id_prefix, discovery, binder)
    if error or not resolved:
        return BindResult(success=False, message=error or "Session not found")

    result = await binder.bind(user_id=user_id, session_id=resolved)
    if result.success:
        conv_status = "✅ conversation available" if result.conversation_available else "⏳ waiting for JSONL"
        return BindResult(
            success=True,
            session_id=resolved,
            message=conv_status,
            conversation_available=result.conversation_available,
        )
    return BindResult(success=False, message=result.message)


@dataclass(frozen=True, slots=True)
class UnbindResult:
    success: bool
    session_id: str | None = None
    message: str = ""


async def resolve_and_unbind(
    session_id_prefix: str,
    *,
    user_id: int,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> UnbindResult:
    """Resolve a session ID prefix and unbind the session from a user.

    Returns an UnbindResult with the outcome. Shared by command and callback handlers.
    """
    resolved, error = _resolve_session_id(session_id_prefix, discovery, binder)
    if error or not resolved:
        return UnbindResult(success=False, message=error or "Session not found")

    result = await binder.unbind(user_id=user_id, session_id=resolved)
    if result.success:
        return UnbindResult(success=True, session_id=resolved)
    return UnbindResult(success=False, message=result.message)
