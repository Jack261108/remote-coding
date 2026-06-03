"""Backward-compatible re-export. Canonical location: app.infra.text_formatting."""

from app.infra.text_formatting import (
    render_markdownish_to_telegram_html,
    split_markdownish_for_telegram,
    split_telegram_html,
)

__all__ = [
    "render_markdownish_to_telegram_html",
    "split_markdownish_for_telegram",
    "split_telegram_html",
]
