from __future__ import annotations

import re
from typing import Any

BRIDGE_MARKER_LINE_RE = re.compile(r"^\s*_*(?:TGCLI_BEGIN|TGCLI_DONE)_*(?:\s*[:：]?\s*[A-Za-z0-9_-]+)?\s*$", re.IGNORECASE)
_BLANK_LINE_BURST_RE = re.compile(r"\n{3,}")
_ANSI_CSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)


def strip_bridge_markers(text: str) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    kept: list[str] = []
    for raw_line in lines:
        if BRIDGE_MARKER_LINE_RE.match(raw_line):
            continue
        kept.append(raw_line)
    return "\n".join(kept)


def strip_ansi_escapes(text: str) -> str:
    if not text:
        return ""
    return _ANSI_CSI_ESCAPE_RE.sub("", _ANSI_OSC_ESCAPE_RE.sub("", text))


def normalize_source_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if not text.strip():
        return ""

    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = strip_bridge_markers(strip_ansi_escapes(cleaned))
    if not cleaned.strip():
        return ""

    normalized_lines = [line.rstrip() for line in cleaned.split("\n")]
    normalized = "\n".join(normalized_lines).strip("\n")
    normalized = _BLANK_LINE_BURST_RE.sub("\n\n", normalized)
    return normalized.strip()


def normalize_prompt_match_text(value: Any) -> str:
    normalized = normalize_source_text(value)
    if not normalized:
        return ""
    command_name = COMMAND_NAME_RE.search(normalized)
    if command_name is not None:
        normalized = command_name.group(1)
    return " ".join(normalized.split())
