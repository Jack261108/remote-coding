from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.session_id_resolver import _resolve_session_id, unavailable_unbound_session_message, unique_prefixes

ExternalSessionAction = Literal["bind", "unbind"]


@dataclass(frozen=True, slots=True)
class ExternalSessionSelectValidation:
    session_id: str | None = None
    cwd: str = ""
    action: ExternalSessionAction | None = None
    callback_token: str = ""
    denial_message: str | None = None


def validate_external_session_select(
    session_id_prefix: str,
    *,
    user_id: int,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> ExternalSessionSelectValidation:
    """Validate which action a sess:select callback may offer."""
    resolved, error = _resolve_session_id(session_id_prefix, discovery, binder)
    prefix = session_id_prefix
    if error or not resolved:
        if error == "Session not found" and discovery.has_unavailable_session_prefix(session_id_prefix):
            return ExternalSessionSelectValidation(denial_message="Session is no longer available")
        return ExternalSessionSelectValidation(denial_message=error or "Session not found")
    if discovery.is_session_unavailable(resolved) or (prefix != resolved and discovery.has_unavailable_session_prefix(session_id_prefix)):
        return ExternalSessionSelectValidation(denial_message="Session is no longer available")

    unavailable = unavailable_unbound_session_message(resolved, discovery)
    if unavailable is not None:
        return ExternalSessionSelectValidation(session_id=resolved, denial_message=unavailable)

    token_candidates = [session.session_id for session in discovery.list_unbound()]
    token_candidates.extend(binding.session_id for binding in binder._binding_store.list_all())
    token_candidates.extend(discovery.unavailable_session_ids())
    callback_token = unique_prefixes(token_candidates, min_length=16, max_length=52)[resolved]

    binding = binder._binding_store.get_binding(resolved)
    if binding is not None:
        if binding.user_id != user_id:
            return ExternalSessionSelectValidation(session_id=resolved, denial_message="Session is not available to bind")
        return ExternalSessionSelectValidation(session_id=resolved, cwd=binding.cwd, action="unbind", callback_token=callback_token)

    unbound_session = discovery.get(resolved)
    cwd = unbound_session.cwd if unbound_session is not None else ""
    return ExternalSessionSelectValidation(session_id=resolved, cwd=cwd, action="bind", callback_token=callback_token)
