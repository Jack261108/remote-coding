from __future__ import annotations

from typing import Any


def render_structured_session(state: Any, *, include_last_reply: bool = True) -> str:
    """Render a structured session summary for Telegram display."""
    lines = [
        "structured_session:",
        f"phase: {state.phase.value}",
        f"turns: {len(state.turns)}",
        f"current_turn_id: {state.current_turn_id or '-'}",
    ]
    if include_last_reply:
        last_turn = state.turns[-1] if state.turns else None
        last_reply = (last_turn.text.strip() if last_turn else "") or "-"
        if len(last_reply) > 200:
            last_reply = f"{last_reply[:200].rstrip()}..."
        lines.append(f"last_reply: {last_reply}")
    return "\n".join(lines)
