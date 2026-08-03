"""Unit tests for ExternalInputQueue (per-session FIFO, cap, TTL, ABA)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.external_input_queue import (
    ExternalInputQueue,
    QueueEnqueueOk,
    QueueEnqueueOverflow,
)


def _clock():
    current = {"t": datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)}

    def _now() -> datetime:
        return current["t"]

    return _now, current


async def test_enqueue_then_dequeue_fifo() -> None:
    now, _ = _clock()
    q = ExternalInputQueue(now=now)
    assert isinstance(await q.enqueue("s1", text="a", binding_id="g"), QueueEnqueueOk)
    assert isinstance((await q.enqueue("s1", text="b", binding_id="g")), QueueEnqueueOk)
    assert (await q.dequeue("s1", binding_id="g")).text == "a"
    assert (await q.dequeue("s1", binding_id="g")).text == "b"
    assert await q.dequeue("s1", binding_id="g") is None


async def test_enqueue_overflow_reports_size() -> None:
    now, _ = _clock()
    q = ExternalInputQueue(max_size=2, now=now)
    await q.enqueue("s1", text="a", binding_id="g")
    await q.enqueue("s1", text="b", binding_id="g")
    result = await q.enqueue("s1", text="c", binding_id="g")
    assert isinstance(result, QueueEnqueueOverflow)
    assert result.size == 2, "overflow reports the cap reached"
    # Cap is honoured — overflow entry was NOT appended.
    assert await q.peek_size("s1") == 2


async def test_dequeue_drops_expired_head() -> None:
    now, current = _clock()
    q = ExternalInputQueue(max_size=5, ttl_sec=10, now=now)
    await q.enqueue("s1", text="old", binding_id="g")
    # Advance past TTL for the enqueued entry.
    current["t"] = now() + timedelta(seconds=20)
    await q.enqueue("s1", text="new", binding_id="g")  # prunes the expired head
    # "old" must have been pruned, only "new" remains.
    assert (await q.dequeue("s1", binding_id="g")).text == "new"


async def test_dequeue_drops_cross_generation_entries() -> None:
    """Entries enqueued under gen-1 are dropped when dequeue is asked for gen-2
    (unbind+rebind ABA) — they are never injected into the new binding."""
    now, _ = _clock()
    q = ExternalInputQueue(now=now)
    await q.enqueue("s1", text="stale", binding_id="gen-1")
    await q.enqueue("s1", text="fresh", binding_id="gen-2")
    assert (await q.dequeue("s1", binding_id="gen-2")).text == "fresh"
    assert await q.dequeue("s1", binding_id="gen-2") is None
    assert await q.peek_size("s1") == 0


async def test_clear_returns_dropped_entries() -> None:
    now, _ = _clock()
    q = ExternalInputQueue(now=now)
    await q.enqueue("s1", text="a", binding_id="g")
    await q.enqueue("s1", text="b", binding_id="g")
    dropped = await q.clear("s1")
    assert [e.text for e in dropped] == ["a", "b"]
    assert await q.peek_size("s1") == 0
    # clearing an unknown session is a no-op empty list.
    assert await q.clear("nope") == []


async def test_dequeue_unknown_session_is_none() -> None:
    now, _ = _clock()
    q = ExternalInputQueue(now=now)
    assert await q.dequeue("missing", binding_id="g") is None


async def test_prune_expired_drops_only_aged_entries() -> None:
    now, current = _clock()
    q = ExternalInputQueue(max_size=5, ttl_sec=10, now=now)
    await q.enqueue("s1", text="old", binding_id="g")
    # Advance 3s and enqueue "mid" — "old" is now 3s old (within TTL).
    current["t"] = now() + timedelta(seconds=3)
    await q.enqueue("s1", text="mid", binding_id="g")
    # Advance another 10s: "old" is 13s (expired), "mid" is 10s (within TTL,
    # we use > strictly so exactly ttl_sec is still live).
    current["t"] = now() + timedelta(seconds=10)
    dropped = await q.prune_expired("s1")
    assert dropped == 1
    assert await q.peek_size("s1") == 1
    assert (await q.dequeue("s1", binding_id="g")).text == "mid"


async def test_queues_are_isolated_per_session() -> None:
    now, _ = _clock()
    q = ExternalInputQueue(max_size=2, now=now)
    await q.enqueue("sA", text="a", binding_id="g")
    await q.enqueue("sB", text="b", binding_id="g")
    assert (await q.dequeue("sA", binding_id="g")).text == "a"
    assert (await q.dequeue("sB", binding_id="g")).text == "b"


def test_constructor_rejects_non_positive_limits() -> None:
    with pytest.raises(ValueError):
        ExternalInputQueue(max_size=0)
    with pytest.raises(ValueError):
        ExternalInputQueue(ttl_sec=0)
