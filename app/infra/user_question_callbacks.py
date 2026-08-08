"""Telegram callback-data helpers for opaque AskUserQuestion tokens."""

from __future__ import annotations

_CALLBACK_DATA_MAX_BYTES = 64
_ALLOWED_PREFIXES = frozenset({"ask", "ext_uq"})


def build_user_question_callback_data(*, prefix: str, token: str) -> str:
    if prefix not in _ALLOWED_PREFIXES:
        raise ValueError(f"unsupported user-question callback prefix: {prefix}")
    if not token or ":" in token:
        raise ValueError("callback token must be non-empty and colon-free")
    data = f"{prefix}:{token}"
    if len(data.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
        raise ValueError("user-question callback_data exceeds Telegram 64-byte limit")
    return data


def parse_user_question_callback_token(data: str | tuple[str, ...] | None, *, prefix: str) -> str | None:
    if prefix not in _ALLOWED_PREFIXES or not data:
        return None
    raw = ":".join(data) if isinstance(data, tuple) else data
    parts = raw.split(":")
    if len(parts) != 2 or parts[0] != prefix or not parts[1]:
        return None
    if len(raw.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
        return None
    return parts[1]
