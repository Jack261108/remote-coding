from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_COMMAND_MAX_CHARS = 300
_DESCRIPTION_MAX_CHARS = 200
_ZWNJ = "\u200c"
_BACKTICK_RUN_RE = re.compile(r"`{3,}")


@dataclass(frozen=True, slots=True)
class PermissionPromptInput:
    tool_name: str
    tool_input: Mapping[str, object] | None
    cwd: str
    session_id: str
    session_title: str | None


class PermissionMessageBuilder:
    def build_permission_prompt(self, prompt: PermissionPromptInput) -> str:
        tool_input = prompt.tool_input or {}
        tool_name = _text(prompt.tool_name)
        command = _truncate(_mapping_text(tool_input, "command"), _COMMAND_MAX_CHARS)
        file_path = _mapping_text(tool_input, "file_path") or _mapping_text(tool_input, "path")
        description = _truncate(_mapping_text(tool_input, "description"), _DESCRIPTION_MAX_CHARS)
        session_label = _code_segment(_text(prompt.session_title)) if prompt.session_title is not None else prompt.session_id[:8]

        cwd = _text(prompt.cwd)
        cwd_label = _code_segment(cwd) if cwd else "unknown"

        lines = [
            f"🔐 [{session_label}] 请求权限: {_code_segment(tool_name)}",
            "",
            "命令:",
            _fenced_code(command),
        ]
        if file_path:
            lines.extend(["", f"文件: {_code_segment(file_path)}"])
        if description:
            lines.extend(["", f"描述: {_code_segment(description)}"])
        lines.extend(["", f"📂 {cwd_label}", "", "请点击下方按钮选择允许或拒绝。"])
        return "\n".join(lines)


def _mapping_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    return _text(value) if value is not None else ""


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _truncate(value: str, max_chars: int) -> str:
    return value[:max_chars]


def _code_segment(value: str) -> str:
    if value == "" or "`" in value or "\n" in value or "\r" in value:
        return _fenced_code(value)
    return f"`{value}`"


def _fenced_code(value: str) -> str:
    return f"```\n{_sanitize_fenced_value(value)}\n```"


def _sanitize_fenced_value(value: str) -> str:
    sanitized = _BACKTICK_RUN_RE.sub(lambda match: _ZWNJ.join("`" for _ in match.group(0)), value)
    if sanitized and (sanitized.startswith(("\n", "\r")) or sanitized.endswith(("\n", "\r"))):
        return f"{_ZWNJ}{sanitized}{_ZWNJ}"
    return sanitized
