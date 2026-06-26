from __future__ import annotations

from aiogram.filters import CommandObject
from aiogram.types import Message


def command_args(command: CommandObject) -> str:
    return (command.args or "").strip()


def split_command_text(text: str | None, *, maxsplit: int) -> list[str]:
    return (text or "").strip().split(maxsplit=maxsplit)


def split_message_command(message: Message, *, maxsplit: int) -> list[str]:
    return split_command_text(message.text, maxsplit=maxsplit)
