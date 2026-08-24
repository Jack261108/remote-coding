from __future__ import annotations

from app.infra.text_formatting import format_external_session_action_outcome, render_markdownish_with_html_fallback


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


class TestRenderMarkdownishWithHtmlFallback:
    def test_short_text_returns_html_chunk(self) -> None:
        chunks, parse_mode = render_markdownish_with_html_fallback("hello world", 4096)
        assert parse_mode == "HTML"
        assert chunks == ["hello world"]
        assert all(len(c) <= 4096 for c in chunks)

    def test_inline_html_path_keeps_html_mode(self) -> None:
        text = "see [link](https://example.com) and `code`"
        chunks, parse_mode = render_markdownish_with_html_fallback(text, 4096)
        assert parse_mode == "HTML"
        assert chunks
        assert all(len(c) <= 4096 for c in chunks)

    def test_empty_text_returns_empty_html(self) -> None:
        chunks, parse_mode = render_markdownish_with_html_fallback("", 4096)
        assert chunks == []
        assert parse_mode == "HTML"

    def test_oversize_html_tag_falls_back_to_plain_text(self) -> None:
        # A markdown link whose URL renders into a single <a> token exceeding max_len.
        long_url = f"https://example.com/{'a' * 5000}"
        text = f"[link]({long_url})"

        chunks, parse_mode = render_markdownish_with_html_fallback(text, 4096)

        assert parse_mode is None
        # Fallback chunks are slices of the ORIGINAL markdownish text at max_len.
        assert "".join(chunks) == text
        for chunk in chunks:
            assert len(chunk) <= 4096

    def test_fallback_chunks_cover_full_text_when_split(self) -> None:
        # Oversized tag plus enough text to require multiple plain-text slices.
        long_url = f"https://example.com/{'a' * 5000}"
        text = f"[link]({long_url})\nmore content after the link"

        chunks, parse_mode = render_markdownish_with_html_fallback(text, 4096)

        assert parse_mode is None
        assert "".join(chunks) == text
        assert len(chunks) >= 2  # text itself > 4096 due to the long URL
        for chunk in chunks:
            assert len(chunk) <= 4096
