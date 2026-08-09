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
    current = {"t": datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC), "m": 0.0}

    def _now() -> datetime:
        return current["t"]

    def _monotonic() -> float:
        return current["m"]

    return _now, _monotonic, current


async def test_enqueue_then_dequeue_fifo() -> None:
    now, mono, _ = _clock()
    q = ExternalInputQueue(now=now, monotonic=mono)
    assert isinstance(await q.enqueue("s1", text="a", binding_id="g"), QueueEnqueueOk)
    assert isinstance((await q.enqueue("s1", text="b", binding_id="g")), QueueEnqueueOk)
    assert (await q.dequeue("s1", binding_id="g")).text == "a"
    assert (await q.dequeue("s1", binding_id="g")).text == "b"
    assert await q.dequeue("s1", binding_id="g") is None


async def test_enqueue_overflow_reports_size() -> None:
    now, mono, _ = _clock()
    q = ExternalInputQueue(max_size=2, now=now, monotonic=mono)
    await q.enqueue("s1", text="a", binding_id="g")
    await q.enqueue("s1", text="b", binding_id="g")
    result = await q.enqueue("s1", text="c", binding_id="g")
    assert isinstance(result, QueueEnqueueOverflow)
    assert result.size == 2, "overflow reports the cap reached"
    # Cap is honoured — overflow entry was NOT appended.
    assert await q.peek_size("s1") == 2


async def test_dequeue_drops_expired_head() -> None:
    now, mono, current = _clock()
    q = ExternalInputQueue(max_size=5, ttl_sec=10, now=now, monotonic=mono)
    await q.enqueue("s1", text="old", binding_id="g")
    # Advance past TTL for the enqueued entry (both clocks in lockstep).
    current["t"] = now() + timedelta(seconds=20)
    current["m"] = 20.0
    await q.enqueue("s1", text="new", binding_id="g")  # prunes the expired head
    # "old" must have been pruned, only "new" remains.
    assert (await q.dequeue("s1", binding_id="g")).text == "new"


async def test_prepend_restores_just_dequeued_entry_to_fifo_head() -> None:
    now, mono, _ = _clock()
    q = ExternalInputQueue(now=now, monotonic=mono)
    await q.enqueue("s1", text="a", binding_id="g")
    await q.enqueue("s1", text="b", binding_id="g")
    first = await q.dequeue("s1", binding_id="g")
    assert first is not None and first.text == "a"
    assert await q.prepend("s1", first)
    assert (await q.dequeue("s1", binding_id="g")).text == "a"
    assert (await q.dequeue("s1", binding_id="g")).text == "b"


async def test_dequeue_drops_cross_generation_entries() -> None:
    """Entries enqueued under gen-1 are dropped when dequeue is asked for gen-2
    (unbind+rebind ABA) — they are never injected into the new binding."""
    now, mono, _ = _clock()
    q = ExternalInputQueue(now=now, monotonic=mono)
    await q.enqueue("s1", text="stale", binding_id="gen-1")
    await q.enqueue("s1", text="fresh", binding_id="gen-2")
    assert (await q.dequeue("s1", binding_id="gen-2")).text == "fresh"
    assert await q.dequeue("s1", binding_id="gen-2") is None
    assert await q.peek_size("s1") == 0


async def test_clear_returns_dropped_entries() -> None:
    now, mono, _ = _clock()
    q = ExternalInputQueue(now=now, monotonic=mono)
    await q.enqueue("s1", text="a", binding_id="g")
    await q.enqueue("s1", text="b", binding_id="g")
    dropped = await q.clear("s1")
    assert [e.text for e in dropped] == ["a", "b"]
    assert await q.peek_size("s1") == 0
    # clearing an unknown session is a no-op empty list.
    assert await q.clear("nope") == []


async def test_dequeue_unknown_session_is_none() -> None:
    now, mono, _ = _clock()
    q = ExternalInputQueue(now=now, monotonic=mono)
    assert await q.dequeue("missing", binding_id="g") is None


async def test_prune_expired_drops_only_aged_entries() -> None:
    now, mono, current = _clock()
    q = ExternalInputQueue(max_size=5, ttl_sec=10, now=now, monotonic=mono)
    await q.enqueue("s1", text="old", binding_id="g")
    # Advance 3s and enqueue "mid" — "old" is now 3s old (within TTL).
    current["t"] = now() + timedelta(seconds=3)
    current["m"] = 3.0
    await q.enqueue("s1", text="mid", binding_id="g")
    # Advance another 10s: "old" is 13s (expired), "mid" is 10s (within TTL,
    # we use > strictly so exactly ttl_sec is still live).
    current["t"] = now() + timedelta(seconds=10)
    current["m"] = 13.0
    dropped = await q.prune_expired("s1")
    assert dropped == 1
    assert await q.peek_size("s1") == 1
    assert (await q.dequeue("s1", binding_id="g")).text == "mid"


async def test_queues_are_isolated_per_session() -> None:
    now, mono, _ = _clock()
    q = ExternalInputQueue(max_size=2, now=now, monotonic=mono)
    await q.enqueue("sA", text="a", binding_id="g")
    await q.enqueue("sB", text="b", binding_id="g")
    assert (await q.dequeue("sA", binding_id="g")).text == "a"
    assert (await q.dequeue("sB", binding_id="g")).text == "b"


def test_constructor_rejects_non_positive_limits() -> None:
    with pytest.raises(ValueError):
        ExternalInputQueue(max_size=0)
    with pytest.raises(ValueError):
        ExternalInputQueue(ttl_sec=0)


async def test_ttl_ignores_wall_clock_jumps_uses_monotonic() -> None:
    """TTL 由 monotonic clock 决定；wall-clock（datetime）跳变不影响过期——

    与 PairingCallbackRegistry/UserQuestionCallbackRegistry 用 time.monotonic 一致，
    避免 NTP step 跳变导致的 TTL 漂移（后跳 → 永不过期；前跳 → 立即过期）。
    """
    now, mono, current = _clock()
    q = ExternalInputQueue(max_size=5, ttl_sec=10, now=now, monotonic=mono)
    await q.enqueue("s1", text="old", binding_id="g")
    # wall-clock 单独前跳 1 小时（模拟 NTP 前跳）——monotonic 未动。
    current["t"] = now() + timedelta(hours=1)
    # "old" 按 monotonic 仍 0s，不该被 prune。
    assert await q.dequeue("s1", binding_id="g") is not None
    assert (await q.dequeue("s1", binding_id="g")) is None

    # 再入队，wall-clock 单独后跳回起点（模拟 NTP 后跳），monotonic 推进过 TTL。
    current["t"] = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
    await q.enqueue("s1", text="x", binding_id="g")
    current["m"] = 11.0  # monotonic 过 TTL
    assert await q.prune_expired("s1") == 1
