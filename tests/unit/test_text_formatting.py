from __future__ import annotations

from app.infra.text_formatting import format_external_session_action_outcome


def test_outcome_bind_success_uses_bound_message() -> None:
    text = format_external_session_action_outcome("bind", True, session_id="abcdefghijklmnop", message="✅ conversation available")

    assert text.startswith("🔗 Bound session abcdefghijkl")
    assert text.endswith("✅ conversation available")


def test_outcome_unbind_success_uses_unbound_message() -> None:
    text = format_external_session_action_outcome("unbind", True, session_id="abcdefghijklmnop", message="")

    assert text == "🔓 Unbound session abcdefghijkl..."


def test_outcome_bind_failure_prefixes_error_message() -> None:
    text = format_external_session_action_outcome("bind", False, session_id=None, message="Session not found")

    assert text == "❌ Session not found"


def test_outcome_unbind_failure_prefixes_error_message() -> None:
    text = format_external_session_action_outcome("unbind", False, session_id="abcdefghijklmnop", message="Session not bound to you")

    assert text == "❌ Session not bound to you"
