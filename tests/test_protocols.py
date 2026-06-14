"""Tests for domain protocols (SessionStoreProtocol).

Covers: runtime_checkable protocol conformance.
"""

from __future__ import annotations

from app.domain.protocols import SessionStoreProtocol


class TestSessionStoreProtocol:
    def test_conforming_class_is_instance(self):
        """A class with all required methods satisfies the protocol."""

        class GoodStore:
            def get_or_create(self, **kwargs):
                pass

            def get(self, session_id):
                pass

            def process(self, event):
                pass

            def mark_interactive_turn_processing(self, **kwargs):
                pass

            def latest_completed_assistant_turn_id(self, **kwargs):
                pass

            def resolve_interactive_session_id(self, **kwargs):
                pass

            def get_interactive_state(self, **kwargs):
                pass

            def interactive_completion_phase(self, **kwargs):
                pass

        store = GoodStore()
        assert isinstance(store, SessionStoreProtocol)

    def test_missing_method_not_instance(self):
        """A class missing required methods does not satisfy the protocol."""

        class BadStore:
            def get(self, session_id):
                pass

        store = BadStore()
        assert not isinstance(store, SessionStoreProtocol)

    def test_empty_class_not_instance(self):
        assert not isinstance(object(), SessionStoreProtocol)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-v"])
