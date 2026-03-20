import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot.presenters.chunk_sender import ChunkSender


@pytest.mark.asyncio
async def test_chunk_sender_split_and_flush() -> None:
    sender = ChunkSender(chunk_size=10, flush_interval_sec=100)
    sent: list[str] = []

    async def send_fn(text: str) -> None:
        sent.append(text)

    await sender.push("abcdefghij", send_fn)
    await sender.push("klm", send_fn)
    await sender.flush(send_fn)

    assert sent == ["abcdefghij", "klm"]


@pytest.mark.asyncio
async def test_chunk_sender_interval_flush() -> None:
    sender = ChunkSender(chunk_size=50, flush_interval_sec=0.001)
    sent: list[str] = []

    async def send_fn(text: str) -> None:
        sent.append(text)

    await sender.push("a", send_fn)
    await asyncio.sleep(0.01)
    await sender.push("b", send_fn)
    await sender.flush(send_fn)

    assert "a" in sent[0]


@pytest.mark.asyncio
async def test_chunk_sender_skips_whitespace_only_payload() -> None:
    sender = ChunkSender(chunk_size=50, flush_interval_sec=100)
    sent: list[str] = []

    async def send_fn(text: str) -> None:
        sent.append(text)

    await sender.push("\n\n   \n", send_fn)
    await sender.flush(send_fn)

    assert sent == []


@pytest.mark.asyncio
async def test_chunk_sender_ignores_non_empty_bad_request() -> None:
    sender = ChunkSender(chunk_size=5, flush_interval_sec=100)
    sent: list[str] = []

    async def send_fn(text: str) -> None:
        sent.append(text)
        if text.strip() == "hello":
            raise TelegramBadRequest(method="sendMessage", message="Bad Request: text must be non-empty")

    await sender.push("hello", send_fn)
    await sender.push("world", send_fn)
    await sender.flush(send_fn)

    assert sent == ["hello", "world"]
