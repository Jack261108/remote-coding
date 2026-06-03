from __future__ import annotations

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
