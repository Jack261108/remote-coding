"""Backward-compatible re-export. Canonical location: app.infra.text_formatting."""

from app.infra.text_formatting import (
    TELEGRAM_TEXT_LIMIT,
    render_markdownish_to_telegram_html,
    render_markdownish_with_html_fallback,
    split_markdownish_for_telegram,
    split_telegram_html,
)

__all__ = [
    "TELEGRAM_TEXT_LIMIT",
    "render_markdownish_to_telegram_html",
    "render_markdownish_with_html_fallback",
    "split_markdownish_for_telegram",
    "split_telegram_html",
]
