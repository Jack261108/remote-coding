from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService

_HASH_TOKEN_PREFIX = "h."


def unique_prefixes(ids: Iterable[str], *, min_length: int = 16, max_length: int = 52) -> dict[str, str]:
    """Return compact callback tokens that resolve uniquely among IDs."""
    candidates = list(dict.fromkeys(ids))
    result: dict[str, str] = {}
    for candidate in candidates:
        start = min(min_length, len(candidate))
        found: str | None = None
        for length in range(start, len(candidate) + 1):
            prefix = candidate[:length]
            if len(prefix) > max_length:
                break
            if prefix == candidate and any(other != candidate and other.startswith(prefix) for other in candidates):
                if len(prefix) + 1 <= max_length:
                    found = f"{prefix}."
                break
            if _is_tmux_user_wide_prefix(prefix):
                continue
            if not any(other != candidate and other.startswith(prefix) for other in candidates):
                found = prefix
                break
        if found is None:
            found = _hash_token(candidate)
        result[candidate] = found
    return result


def resolve_unique_prefix(prefix: str, candidates: Iterable[str]) -> tuple[str | None, str | None]:
    """Resolve a callback token against candidates without silently widening stale prefixes."""
    token = prefix.rstrip(".")
    candidate_list = list(dict.fromkeys(candidates))
    if prefix.endswith("."):
        matches = [candidate for candidate in candidate_list if candidate == token]
    elif prefix.startswith(_HASH_TOKEN_PREFIX):
        matches = [candidate for candidate in candidate_list if _hash_token(candidate) == prefix]
    elif _is_tmux_user_wide_prefix(prefix):
        matches = []
    else:
        matches = [candidate for candidate in candidate_list if candidate == prefix or candidate.startswith(prefix)]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) == 0:
        return None, "Session not found"
    return None, f"Ambiguous prefix, {len(matches)} matches. Be more specific."


def _hash_token(value: str) -> str:
    return f"{_HASH_TOKEN_PREFIX}{hashlib.sha1(value.encode()).hexdigest()[:16]}"


def _is_tmux_user_wide_prefix(prefix: str) -> bool:
    parts = prefix.split("_")
    return len(parts) == 3 and parts[0] == "user" and parts[1].isdigit() and parts[2] == ""


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

    for b in binder._binding_store.list_all():
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
