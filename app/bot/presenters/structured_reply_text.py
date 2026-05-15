from __future__ import annotations

import re

_MARKER_LINE_RE = re.compile(r"^\s*_*(?:TGCLI_BEGIN|TGCLI_DONE)_*(?:\s*[:：]?\s*[A-Za-z0-9_-]+)?\s*$", re.IGNORECASE)
_BLANK_LINE_BURST_RE = re.compile(r"\n{3,}")
_STREAM_PREVIEW_CHAR_LIMIT = 1800
_STREAM_PREVIEW_LINE_LIMIT = 60
_PERMISSION_INPUT_CHAR_LIMIT = 280
_PERMISSION_INPUT_LINE_LIMIT = 8
_QUESTION_TEXT_CHAR_LIMIT = 360
_QUESTION_TEXT_LINE_LIMIT = 10


def strip_bridge_markers(text: str) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    kept: list[str] = []
    for raw_line in lines:
        if _MARKER_LINE_RE.match(raw_line):
            continue
        kept.append(raw_line)
    return "\n".join(kept)


def normalize_stream_text(text: str) -> str:
    cleaned = strip_bridge_markers(text).replace("\r\n", "\n").replace("\r", "\n")
    if not cleaned.strip():
        return ""

    normalized_lines = [line.rstrip() for line in cleaned.split("\n")]
    normalized = "\n".join(normalized_lines).strip("\n")
    normalized = _BLANK_LINE_BURST_RE.sub("\n\n", normalized)
    return normalized.strip()


def _truncate_text(text: str, *, char_limit: int, line_limit: int, suffix: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    lines = normalized.split("\n")
    needs_line_truncation = len(lines) > line_limit
    preview_lines = lines[:line_limit]
    preview = "\n".join(preview_lines)

    needs_char_truncation = len(preview) > char_limit
    if needs_char_truncation:
        preview = preview[:char_limit].rstrip()

    if needs_line_truncation or needs_char_truncation:
        preview = f"{preview}{suffix}"
    return preview


def preview_stream_text(text: str) -> str:
    return _truncate_text(
        normalize_stream_text(text),
        char_limit=_STREAM_PREVIEW_CHAR_LIMIT,
        line_limit=_STREAM_PREVIEW_LINE_LIMIT,
        suffix="\n...[输出片段过长，已截断本条消息]",
    )


def _truncate_permission_text(text: str) -> str:
    return _truncate_text(
        text,
        char_limit=_PERMISSION_INPUT_CHAR_LIMIT,
        line_limit=_PERMISSION_INPUT_LINE_LIMIT,
        suffix="...",
    )


def _truncate_question_text(text: str) -> str:
    return _truncate_text(
        text,
        char_limit=_QUESTION_TEXT_CHAR_LIMIT,
        line_limit=_QUESTION_TEXT_LINE_LIMIT,
        suffix="...",
    )
