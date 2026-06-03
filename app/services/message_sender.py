from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    callback_data: str


@dataclass(frozen=True, slots=True)
class Keyboard:
    rows: list[list[Button]]


@runtime_checkable
class MessageSender(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        parse_mode: str | None = None,
    ) -> None: ...

    async def send_photo(
        self,
        chat_id: int,
        file_path: Path,
        caption: str = "",
    ) -> None: ...

    async def send_document(
        self,
        chat_id: int,
        file_path: Path,
        caption: str = "",
    ) -> None: ...
