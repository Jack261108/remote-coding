import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot.presenters.chunk_sender import ChunkSender
from app.bot.presenters.telegram_formatting import split_telegram_html


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

    assert sent == ["a"]

    await sender.push("b", send_fn)
    await sender.flush(send_fn)

    assert sent == ["a", "b"]


@pytest.mark.asyncio
async def test_chunk_sender_batches_burst_before_delayed_flush() -> None:
    sender = ChunkSender(chunk_size=50, flush_interval_sec=0.02)
    sent: list[str] = []

    async def send_fn(text: str) -> None:
        sent.append(text)

    await sender.push("hello", send_fn)
    await sender.push(" world", send_fn)
    await asyncio.sleep(0.03)

    assert sent == ["hello world"]


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

    assert await sender.push("hello", send_fn) is False
    assert await sender.push("world", send_fn) is True
    assert await sender.flush(send_fn) is True

    assert sent == ["hello", "world"]


@pytest.mark.asyncio
async def test_chunk_sender_push_immediate_send_success_returns_true() -> None:
    sender = ChunkSender(chunk_size=5, flush_interval_sec=100)
    sent: list[str] = []

    async def send_fn(text: str) -> bool:
        sent.append(text)
        return True

    assert await sender.push("hello", send_fn) is True
    assert sent == ["hello"]


@pytest.mark.asyncio
async def test_chunk_sender_push_immediate_send_failure_returns_false() -> None:
    sender = ChunkSender(chunk_size=5, flush_interval_sec=100)
    sent: list[str] = []

    async def send_fn(text: str) -> bool:
        sent.append(text)
        return False

    assert await sender.push("hello", send_fn) is False
    assert sent == ["hello"]


@pytest.mark.asyncio
async def test_chunk_sender_flush_buffered_send_failure_returns_false() -> None:
    sender = ChunkSender(chunk_size=50, flush_interval_sec=100)
    sent: list[str] = []

    async def send_fn(text: str) -> bool:
        sent.append(text)
        return False

    assert await sender.push("hello", send_fn) is True
    assert await sender.flush(send_fn) is False
    assert sent == ["hello"]


@pytest.mark.asyncio
async def test_chunk_sender_treats_none_send_result_as_success() -> None:
    sender = ChunkSender(chunk_size=5, flush_interval_sec=100)
    sent: list[str] = []

    async def send_fn(text: str) -> None:
        sent.append(text)

    assert await sender.push("hello", send_fn) is True
    assert sent == ["hello"]


@pytest.mark.asyncio
async def test_chunk_sender_too_long_split_propagates_child_failure() -> None:
    sender = ChunkSender(chunk_size=10, flush_interval_sec=100)
    sent: list[str] = []

    async def send_fn(text: str) -> bool:
        sent.append(text)
        if text == "abcdefghij":
            raise TelegramBadRequest(method="sendMessage", message="Bad Request: message is too long")
        return text != "abcde"

    assert await sender.push("abcdefghij", send_fn) is False
    assert sent == ["abcdefghij", "abcde", "fghij"]


@pytest.mark.asyncio
async def test_chunk_sender_preserves_fenced_code_blocks_when_splitting() -> None:
    sender = ChunkSender(chunk_size=24, flush_interval_sec=100)
    sent: list[str] = []

    async def send_fn(text: str) -> None:
        sent.append(text)

    await sender.push("```python\n1234567890\nabcdefghij\n```", send_fn)
    await sender.flush(send_fn)

    assert len(sent) == 2
    assert all(chunk.startswith("```python\n") for chunk in sent)
    assert all(chunk.endswith("```") for chunk in sent)
    assert sent == ["```python\n1234567890\n```", "```python\nabcdefghij\n```"]


def test_split_telegram_html_closes_and_reopens_tags_for_long_code_blocks() -> None:
    chunks = split_telegram_html("<pre><code>1234567890abcdefghij</code></pre>", 34)

    assert chunks == [
        "<pre><code>1234567890</code></pre>",
        "<pre><code>abcdefghij</code></pre>",
    ]
