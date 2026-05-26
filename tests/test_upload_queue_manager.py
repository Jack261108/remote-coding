from __future__ import annotations


from app.services.upload_queue_manager import UploadQueueManager


def test_enqueue_success() -> None:
    mgr = UploadQueueManager(max_files_per_user=3, max_bytes_per_user=1000)
    ok, msg = mgr.enqueue(1, "a.txt", b"hello", 5)
    assert ok is True
    assert msg == "queued"


def test_enqueue_disabled_when_max_files_zero() -> None:
    mgr = UploadQueueManager(max_files_per_user=0, max_bytes_per_user=0)
    ok, msg = mgr.enqueue(1, "a.txt", b"hello", 5)
    assert ok is False
    assert msg == "queuing disabled"


def test_enqueue_rejects_when_file_count_exceeded() -> None:
    mgr = UploadQueueManager(max_files_per_user=2, max_bytes_per_user=10000)
    mgr.enqueue(1, "a.txt", b"a", 1)
    mgr.enqueue(1, "b.txt", b"b", 1)
    ok, msg = mgr.enqueue(1, "c.txt", b"c", 1)
    assert ok is False
    assert "queue full" in msg


def test_enqueue_rejects_when_byte_limit_exceeded() -> None:
    mgr = UploadQueueManager(max_files_per_user=10, max_bytes_per_user=10)
    mgr.enqueue(1, "a.txt", b"x" * 8, 8)
    ok, msg = mgr.enqueue(1, "b.txt", b"x" * 5, 5)
    assert ok is False
    assert "byte limit" in msg


def test_drain_returns_fifo_order() -> None:
    mgr = UploadQueueManager(max_files_per_user=5, max_bytes_per_user=1000)
    mgr.enqueue(1, "first.txt", b"1", 1)
    mgr.enqueue(1, "second.txt", b"2", 1)
    mgr.enqueue(1, "third.txt", b"3", 1)

    items = mgr.drain(1)
    assert [i.filename for i in items] == ["first.txt", "second.txt", "third.txt"]


def test_drain_clears_queue() -> None:
    mgr = UploadQueueManager(max_files_per_user=5, max_bytes_per_user=1000)
    mgr.enqueue(1, "a.txt", b"a", 1)
    mgr.drain(1)

    # Queue should be empty, so new enqueue succeeds and drain returns nothing extra
    assert mgr.drain(1) == []


def test_drain_empty_user() -> None:
    mgr = UploadQueueManager(max_files_per_user=5, max_bytes_per_user=1000)
    assert mgr.drain(99) == []


def test_is_full_when_at_limit() -> None:
    mgr = UploadQueueManager(max_files_per_user=2, max_bytes_per_user=10000)
    assert mgr.is_full(1) is False
    mgr.enqueue(1, "a.txt", b"a", 1)
    assert mgr.is_full(1) is False
    mgr.enqueue(1, "b.txt", b"b", 1)
    assert mgr.is_full(1) is True


def test_is_full_when_disabled() -> None:
    mgr = UploadQueueManager(max_files_per_user=0, max_bytes_per_user=0)
    assert mgr.is_full(1) is True


def test_per_user_isolation() -> None:
    mgr = UploadQueueManager(max_files_per_user=1, max_bytes_per_user=100)
    ok1, _ = mgr.enqueue(1, "a.txt", b"a", 1)
    ok2, _ = mgr.enqueue(2, "b.txt", b"b", 1)
    assert ok1 is True
    assert ok2 is True

    # User 1 is full, user 2 is also full (limit is 1)
    assert mgr.is_full(1) is True
    assert mgr.is_full(2) is True


def test_drain_resets_byte_total() -> None:
    mgr = UploadQueueManager(max_files_per_user=5, max_bytes_per_user=10)
    mgr.enqueue(1, "a.txt", b"x" * 8, 8)
    mgr.drain(1)
    # After drain, byte total is reset so we can enqueue again
    ok, _ = mgr.enqueue(1, "b.txt", b"x" * 8, 8)
    assert ok is True
