from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PermissionPromptInput:
    tool_name: str
    tool_input: Mapping[str, object] | None
    cwd: str
    session_id: str
    session_title: str | None
