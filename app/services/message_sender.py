from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    callback_data: str


@dataclass(frozen=True, slots=True)
class Keyboard:
    rows: list[list[Button]]


class BaseMessageSender(ABC):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        parse_mode: str | None = None,
    ) -> int | None:
        message = await self._send_or_edit_message(
            chat_id=chat_id,
            text=text,
            keyboard=keyboard,
            parse_mode=parse_mode,
        )
        message_id = getattr(message, "message_id", None)
        return message_id if isinstance(message_id, int) else None

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        parse_mode: str | None = None,
    ) -> None:
        await self._send_or_edit_message(
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            keyboard=keyboard,
            parse_mode=parse_mode,
        )

    async def send_photo(
        self,
        chat_id: int,
        file_path: Path,
        caption: str = "",
    ) -> None:
        await self._send_media(chat_id, file_path, "photo", caption)

    async def send_document(
        self,
        chat_id: int,
        file_path: Path,
        caption: str = "",
    ) -> None:
        await self._send_media(chat_id, file_path, "document", caption)

    @abstractmethod
    async def _send_media(
        self,
        chat_id: int,
        media: Any,
        media_type: str,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> Any:
        """通用媒体发送方法。"""
        raise NotImplementedError

    @abstractmethod
    async def _send_or_edit_message(
        self,
        chat_id: int,
        text: str,
        message_id: int | None = None,
        parse_mode: str | None = "HTML",
        *,
        keyboard: Keyboard | None = None,
    ) -> Any:
        """通用消息发送/编辑方法。"""
        raise NotImplementedError


@runtime_checkable
class MessageSender(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        parse_mode: str | None = None,
    ) -> int | None: ...

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
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
