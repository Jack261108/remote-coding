"""Unit tests for SessionTombstoneStore."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.session_tombstone import SessionTombstoneStore


@pytest.fixture
def store() -> SessionTombstoneStore:
    return SessionTombstoneStore(ttl_seconds=60)


# -- 核心功能 --


class TestMarkEnded:
    def test_mark_ended_makes_is_ended_true(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        assert store.is_ended("s1")

    def test_mark_ended_removes_from_unavailable(self, store: SessionTombstoneStore) -> None:
        store.mark_unavailable("s1")
        store.mark_ended("s1")
        assert store.is_ended("s1")
        assert not store.is_unavailable("s1")

    def test_mark_ended_returns_ids(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        store.mark_ended("s2")
        assert store.ended_ids() == {"s1", "s2"}


class TestMarkUnavailable:
    def test_mark_unavailable_makes_is_unavailable_true(self, store: SessionTombstoneStore) -> None:
        store.mark_unavailable("s1")
        assert store.is_unavailable("s1")

    def test_mark_unavailable_returns_ids(self, store: SessionTombstoneStore) -> None:
        store.mark_unavailable("s1")
        store.mark_unavailable("s2")
        assert store.unavailable_ids() == {"s1", "s2"}


class TestClear:
    def test_clear_removes_both_states(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        store.mark_unavailable("s2")
        store.clear("s1")
        store.clear("s2")
        assert not store.is_ended("s1")
        assert not store.is_unavailable("s2")

    def test_clear_nonexistent_is_noop(self, store: SessionTombstoneStore) -> None:
        store.clear("nonexistent")  # should not raise


class TestIsEnded:
    def test_unknown_session_returns_false(self, store: SessionTombstoneStore) -> None:
        assert not store.is_ended("unknown")

    def test_unavailable_session_returns_false(self, store: SessionTombstoneStore) -> None:
        store.mark_unavailable("s1")
        assert not store.is_ended("s1")


class TestIsUnavailable:
    def test_unknown_session_returns_false(self, store: SessionTombstoneStore) -> None:
        assert not store.is_unavailable("unknown")

    def test_ended_session_returns_false(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        assert not store.is_unavailable("s1")


# -- TTL 过期 --


class TestTTL:
    def test_expired_ended_returns_false(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        # Simulate time passing beyond TTL
        store._ended["s1"] = datetime.now(UTC) - timedelta(seconds=120)
        assert not store.is_ended("s1")

    def test_expired_ended_removes_from_dict(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        store._ended["s1"] = datetime.now(UTC) - timedelta(seconds=120)
        store.is_ended("s1")
        assert "s1" not in store._ended

    def test_expired_unavailable_returns_false(self, store: SessionTombstoneStore) -> None:
        store.mark_unavailable("s1")
        store._unavailable["s1"] = datetime.now(UTC) - timedelta(seconds=120)
        assert not store.is_unavailable("s1")

    def test_expired_unavailable_removes_from_dict(self, store: SessionTombstoneStore) -> None:
        store.mark_unavailable("s1")
        store._unavailable["s1"] = datetime.now(UTC) - timedelta(seconds=120)
        store.is_unavailable("s1")
        assert "s1" not in store._unavailable

    def test_not_yet_expired_returns_true(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        store._ended["s1"] = datetime.now(UTC) - timedelta(seconds=30)
        assert store.is_ended("s1")

    def test_cleanup_expired_removes_stale_entries(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        store.mark_unavailable("s2")
        store._ended["s1"] = datetime.now(UTC) - timedelta(seconds=120)
        store._unavailable["s2"] = datetime.now(UTC) - timedelta(seconds=120)
        store.cleanup_expired()
        assert "s1" not in store._ended
        assert "s2" not in store._unavailable

    def test_ended_ids_cleans_expired(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        store.mark_ended("s2")
        store._ended["s1"] = datetime.now(UTC) - timedelta(seconds=120)
        result = store.ended_ids()
        assert "s1" not in result
        assert "s2" in result

    def test_unavailable_ids_cleans_expired(self, store: SessionTombstoneStore) -> None:
        store.mark_unavailable("s1")
        store.mark_unavailable("s2")
        store._unavailable["s1"] = datetime.now(UTC) - timedelta(seconds=120)
        result = store.unavailable_ids()
        assert "s1" not in result
        assert "s2" in result


# -- 边界条件 --


class TestEdgeCases:
    def test_default_ttl(self) -> None:
        s = SessionTombstoneStore()
        assert s._ttl == timedelta(seconds=3600)

    def test_overwrite_same_session(self, store: SessionTombstoneStore) -> None:
        store.mark_ended("s1")
        store.mark_ended("s1")
        assert store.is_ended("s1")
        assert len(store._ended) == 1

    def test_empty_ids(self, store: SessionTombstoneStore) -> None:
        assert store.ended_ids() == set()
        assert store.unavailable_ids() == set()

    def test_mark_unavailable_does_not_clear_ended(self, store: SessionTombstoneStore) -> None:
        """mark_unavailable does not remove from ended dict."""
        store.mark_ended("s1")
        store.mark_unavailable("s1")
        assert store.is_unavailable("s1")
        assert store.is_ended("s1")  # still in ended dict

    def test_mark_ended_clears_unavailable(self, store: SessionTombstoneStore) -> None:
        """mark_ended removes from unavailable dict."""
        store.mark_unavailable("s1")
        store.mark_ended("s1")
        assert store.is_ended("s1")
        assert not store.is_unavailable("s1")
