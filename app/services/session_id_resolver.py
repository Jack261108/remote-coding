from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from app.services.external_session_binder import ExternalSessionBinder
from app.services.external_session_discovery import ExternalSessionDiscoveryService
from app.services.process_liveness import process_is_alive

_HASH_TOKEN_PREFIX = "~"
_LEGACY_HASH_TOKEN_PREFIX = "h."

logger = logging.getLogger(__name__)


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
                break
            if _is_tmux_user_wide_prefix(prefix):
                continue
            if _is_callback_token_collision(prefix):
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
    candidate_list = list(dict.fromkeys(candidates))
    if _is_hash_token_shape(prefix, prefix=_HASH_TOKEN_PREFIX):
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


def _legacy_hash_token(value: str) -> str:
    return f"{_LEGACY_HASH_TOKEN_PREFIX}{hashlib.sha1(value.encode()).hexdigest()[:16]}"


def _is_callback_token_collision(token: str) -> bool:
    if token.endswith("."):
        return True
    if _is_hash_token_shape(token, prefix=_LEGACY_HASH_TOKEN_PREFIX):
        return True
    if _is_hash_token_shape(token, prefix=_HASH_TOKEN_PREFIX):
        return True
    return False


def _is_hash_token_shape(token: str, *, prefix: str) -> bool:
    digest = token.removeprefix(prefix)
    return len(digest) == 16 and token.startswith(prefix) and all(char in "0123456789abcdef" for char in digest)


def _is_tmux_user_wide_prefix(prefix: str) -> bool:
    parts = prefix.split("_")
    return len(parts) == 3 and parts[0] == "user" and parts[1].isdigit() and parts[2] == ""


def unavailable_unbound_session_message(session_id: str, discovery: ExternalSessionDiscoveryService) -> str | None:
    """Return an unavailable message when an unbound session is stale or dead."""
    unbound = discovery.get(session_id)
    if unbound is None:
        return None
    if discovery.is_session_stale(session_id):
        return "Session is no longer available"
    if unbound.pid is not None and unbound.pid > 0:
        try:
            if not process_is_alive(unbound.pid):
                return "Session is no longer available"
        except Exception:
            logger.warning(
                "failed to check unbound external session pid during session resolution",
                extra={"session_id": session_id, "pid": unbound.pid},
                exc_info=True,
            )
    return None


def _resolve_session_id(
    session_id_prefix: str,
    discovery: ExternalSessionDiscoveryService,
    binder: ExternalSessionBinder,
) -> tuple[str | None, str | None]:
    """Resolve a partial session_id prefix to a full session_id.

    Searches both unbound discovery list and bound sessions.
    Returns (full_session_id, error_message). If ambiguous, returns error.
    """
    candidate_ids = [s.session_id for s in discovery.list_unbound()]
    candidate_ids.extend(b.session_id for b in binder._binding_store.list_all())
    unavailable_ids = discovery.unavailable_session_ids()
    if _is_hash_token_shape(session_id_prefix, prefix=_HASH_TOKEN_PREFIX):
        resolved_token, token_error = resolve_unique_prefix(
            session_id_prefix,
            [*candidate_ids, *unavailable_ids],
        )
        return resolved_token, token_error
    legacy_hash_matches = [
        session_id for session_id in [*candidate_ids, *unavailable_ids] if _legacy_hash_token(session_id) == session_id_prefix
    ]
    if legacy_hash_matches:
        return legacy_hash_matches[0], None
    if session_id_prefix.endswith("."):
        legacy_exact = session_id_prefix[:-1]
        if legacy_exact in [*candidate_ids, *unavailable_ids]:
            return legacy_exact, None

    prefix = session_id_prefix
    candidates: list[str] = []
    unavailable_candidates: list[str] = []

    for s in discovery.list_unbound():
        if s.session_id == prefix or s.session_id.startswith(prefix):
            if unavailable_unbound_session_message(s.session_id, discovery) is None:
                candidates.append(s.session_id)
            elif s.session_id not in unavailable_candidates:
                unavailable_candidates.append(s.session_id)

    for session_id in discovery.unavailable_session_ids():
        if (session_id == prefix or session_id.startswith(prefix)) and session_id not in unavailable_candidates:
            unavailable_candidates.append(session_id)

    for b in binder._binding_store.list_all():
        if b.session_id == prefix or b.session_id.startswith(prefix):
            if b.session_id not in candidates:
                candidates.append(b.session_id)

    if len(candidates) > 1:
        return None, f"Ambiguous prefix, {len(candidates)} matches. Be more specific."
    if len(unavailable_candidates) > 0:
        exact_unavailable = [session_id for session_id in unavailable_candidates if session_id == prefix]
        return (exact_unavailable[0] if exact_unavailable else unavailable_candidates[0]), None
    if len(candidates) == 1:
        return candidates[0], None
    return None, "Session not found"


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
    prefix = session_id_prefix
    if error or not resolved:
        if error == "Session not found" and discovery.has_unavailable_session_prefix(session_id_prefix):
            return BindResult(success=False, message="Session is no longer available")
        return BindResult(success=False, message=error or "Session not found")
    if discovery.is_session_unavailable(resolved) or (prefix != resolved and discovery.has_unavailable_session_prefix(session_id_prefix)):
        return BindResult(success=False, message="Session is no longer available")

    unavailable = unavailable_unbound_session_message(resolved, discovery)
    if unavailable is not None:
        return BindResult(success=False, message=unavailable)
    if discovery.get(resolved) is None and binder._binding_store.get_binding(resolved) is not None:
        return BindResult(success=False, message="Session is not available to bind")

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
    prefix = session_id_prefix
    if error or not resolved:
        if error == "Session not found" and discovery.has_unavailable_session_prefix(session_id_prefix):
            return UnbindResult(success=False, message="Session is no longer available")
        return UnbindResult(success=False, message=error or "Session not found")
    if discovery.is_session_unavailable(resolved) or (prefix != resolved and discovery.has_unavailable_session_prefix(session_id_prefix)):
        return UnbindResult(success=False, message="Session is no longer available")

    result = await binder.unbind(user_id=user_id, session_id=resolved)
    if result.success:
        return UnbindResult(success=True, session_id=resolved)
    return UnbindResult(success=False, message=result.message)
