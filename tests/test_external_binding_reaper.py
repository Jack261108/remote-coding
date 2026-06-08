"""Unit tests for ExternalBindingReaper.remove_with_cleanup.

Spec: external-binding-pid-liveness (task 7.3)

These tests pin two behaviors of the shared removal collaborator:

- Canonical cleanup order: a removable binding is unwound as
  ``remove_binding`` -> ``clear_session`` -> ``cancel_pending_permissions``
  in EXACTLY that sequence (Req 6.4). The order must live in one place and be
  identical regardless of which path (cleanup loop or `/list`) drives it
  (Req 9.2).
- Re-read guard: calling ``remove_with_cleanup`` for a session that is no
  longer present (a concurrent ``SessionEnd`` removed it) returns ``False``
  and performs no associated-state cleanup (Req 6.6, 9.2).

**Validates: Requirements 6.4, 6.6, 9.2**
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.external_session_models import ExternalBinding
from app.domain.models import utc_now
from app.services.external_binding_reaper import ExternalBindingReaper


def _make_binding(session_id: str = "sess-1") -> ExternalBinding:
    """A fully-populated binding so the reaper's INFO log can read every
    context field (user_id, cwd, bound_at, last_activity_at, pid)."""
    return ExternalBinding(
        session_id=session_id,
        user_id=7,
        cwd="/home/user/project",
        bound_at=utc_now(),
        jsonl_path=None,
        pid=1234,
    )


# --- Canonical order (Req 6.4) ----------------------------------------------


async def test_remove_with_cleanup_calls_collaborators_in_canonical_order() -> None:
    """**Validates: Requirements 6.4, 9.2**

    With a binding present, ``remove_with_cleanup(reason="pid_dead")`` must
    invoke ``remove_binding`` -> ``clear_session`` ->
    ``cancel_pending_permissions`` in exactly that order. A single ``Mock``
    manager records the cross-collaborator call sequence; the sync
    ``remove_binding`` is a plain ``Mock`` and the two awaited collaborators
    are ``AsyncMock``s attached to the same manager.
    """
    manager = Mock()
    manager.attach_mock(Mock(), "remove_binding")
    manager.attach_mock(AsyncMock(), "clear_session")
    manager.attach_mock(AsyncMock(), "cancel_pending_permissions")

    binding = _make_binding("sess-order")

    # get_binding must return a real binding so the re-read guard passes and
    # the INFO log can read its fields; it is intentionally NOT routed through
    # the manager so it does not pollute the recorded call order.
    binding_store = Mock()
    binding_store.get_binding = Mock(return_value=binding)
    binding_store.remove_binding = manager.remove_binding

    auto_approve_service = Mock()
    auto_approve_service.clear_session = manager.clear_session

    hook_socket_server = Mock()
    hook_socket_server.cancel_pending_permissions = manager.cancel_pending_permissions

    reaper = ExternalBindingReaper(
        binding_store=binding_store,
        auto_approve_service=auto_approve_service,
        hook_socket_server=hook_socket_server,
    )

    result = await reaper.remove_with_cleanup("sess-order", reason="pid_dead")

    assert result is True

    call_names = [c[0] for c in manager.mock_calls]
    assert call_names == ["remove_binding", "clear_session", "cancel_pending_permissions"]

    manager.remove_binding.assert_called_once_with("sess-order")
    manager.clear_session.assert_awaited_once_with("sess-order")
    manager.cancel_pending_permissions.assert_awaited_once_with(session_id="sess-order")


# --- Re-read guard (Req 6.6, 9.2) -------------------------------------------


async def test_remove_with_cleanup_invalidates_optional_lifecycle_state() -> None:
    binding_store = Mock()
    binding_store.get_binding = Mock(return_value=_make_binding("sess-lifecycle"))
    binding_store.remove_binding = Mock()

    manager = Mock()
    manager.attach_mock(AsyncMock(return_value=1), "invalidate_session")
    manager.attach_mock(Mock(return_value=1), "invalidate_user_questions")
    manager.attach_mock(Mock(), "mark_session_ended")
    manager.attach_mock(AsyncMock(), "clear_session")
    manager.attach_mock(AsyncMock(), "cancel_pending_permissions")

    auto_approve_service = Mock()
    auto_approve_service.clear_session = manager.clear_session
    hook_socket_server = Mock()
    hook_socket_server.cancel_pending_permissions = manager.cancel_pending_permissions
    permission_callback_registry = Mock()
    permission_callback_registry.invalidate_session = manager.invalidate_session
    external_uq_state = Mock()
    external_uq_state.invalidate_session = manager.invalidate_user_questions
    external_discovery = Mock()
    external_discovery.mark_session_ended = manager.mark_session_ended

    reaper = ExternalBindingReaper(
        binding_store=binding_store,
        auto_approve_service=auto_approve_service,
        hook_socket_server=hook_socket_server,
        permission_callback_registry=permission_callback_registry,
        external_uq_state=external_uq_state,
        external_discovery=external_discovery,
    )

    result = await reaper.remove_with_cleanup("sess-lifecycle", reason="pid_dead")

    assert result is True
    permission_callback_registry.invalidate_session.assert_awaited_once_with("sess-lifecycle")
    external_uq_state.invalidate_session.assert_called_once_with("sess-lifecycle")
    external_discovery.mark_session_ended.assert_called_once_with("sess-lifecycle")
    assert [call[0] for call in manager.mock_calls] == [
        "mark_session_ended",
        "invalidate_session",
        "invalidate_user_questions",
        "clear_session",
        "cancel_pending_permissions",
    ]


async def test_remove_with_cleanup_tombstones_before_awaiting_pid_dead_cleanup() -> None:
    binding_store = Mock()
    binding_store.get_binding = Mock(return_value=_make_binding("sess-tombstone-before-await"))
    binding_store.remove_binding = Mock()

    auto_approve_service = Mock()
    auto_approve_service.clear_session = AsyncMock()
    hook_socket_server = Mock()
    hook_socket_server.cancel_pending_permissions = AsyncMock()
    external_discovery = Mock()
    tombstone_observed_before_await = False

    async def invalidate_session(session_id: str) -> int:
        nonlocal tombstone_observed_before_await
        tombstone_observed_before_await = external_discovery.mark_session_ended.called
        return 1

    permission_callback_registry = Mock()
    permission_callback_registry.invalidate_session = AsyncMock(side_effect=invalidate_session)
    external_uq_state = Mock()
    external_uq_state.invalidate_session = Mock(return_value=1)

    reaper = ExternalBindingReaper(
        binding_store=binding_store,
        auto_approve_service=auto_approve_service,
        hook_socket_server=hook_socket_server,
        permission_callback_registry=permission_callback_registry,
        external_uq_state=external_uq_state,
        external_discovery=external_discovery,
    )

    result = await reaper.remove_with_cleanup("sess-tombstone-before-await", reason="pid_dead")

    assert result is True
    assert tombstone_observed_before_await is True
    external_discovery.mark_session_ended.assert_called_once_with("sess-tombstone-before-await")


async def test_remove_with_cleanup_continues_after_optional_cleanup_failure() -> None:
    binding_store = Mock()
    binding_store.get_binding = Mock(return_value=_make_binding("sess-cleanup-failure"))
    binding_store.remove_binding = Mock()

    auto_approve_service = Mock()
    auto_approve_service.clear_session = AsyncMock()
    hook_socket_server = Mock()
    hook_socket_server.cancel_pending_permissions = AsyncMock()
    permission_callback_registry = Mock()
    permission_callback_registry.invalidate_session = AsyncMock(side_effect=RuntimeError("registry failure"))
    external_uq_state = Mock()
    external_uq_state.invalidate_session = Mock(return_value=1)
    external_discovery = Mock()
    external_discovery.mark_session_ended = Mock()

    reaper = ExternalBindingReaper(
        binding_store=binding_store,
        auto_approve_service=auto_approve_service,
        hook_socket_server=hook_socket_server,
        permission_callback_registry=permission_callback_registry,
        external_uq_state=external_uq_state,
        external_discovery=external_discovery,
    )

    result = await reaper.remove_with_cleanup("sess-cleanup-failure", reason="pid_dead")

    assert result is True
    binding_store.remove_binding.assert_called_once_with("sess-cleanup-failure")
    permission_callback_registry.invalidate_session.assert_awaited_once_with("sess-cleanup-failure")
    external_uq_state.invalidate_session.assert_called_once_with("sess-cleanup-failure")
    external_discovery.mark_session_ended.assert_called_once_with("sess-cleanup-failure")
    auto_approve_service.clear_session.assert_awaited_once_with("sess-cleanup-failure")
    hook_socket_server.cancel_pending_permissions.assert_awaited_once_with(session_id="sess-cleanup-failure")


async def test_remove_with_cleanup_skips_when_binding_already_absent() -> None:
    """**Validates: Requirements 6.6, 9.2**

    When the re-read via ``get_binding`` returns ``None`` (the binding was
    already removed by a concurrent path), ``remove_with_cleanup`` returns
    ``False`` and performs NO ``remove_binding``, NO ``clear_session``, and NO
    ``cancel_pending_permissions``.
    """
    binding_store = Mock()
    binding_store.get_binding = Mock(return_value=None)
    binding_store.remove_binding = Mock()

    auto_approve_service = Mock()
    auto_approve_service.clear_session = AsyncMock()

    hook_socket_server = Mock()
    hook_socket_server.cancel_pending_permissions = AsyncMock()

    reaper = ExternalBindingReaper(
        binding_store=binding_store,
        auto_approve_service=auto_approve_service,
        hook_socket_server=hook_socket_server,
    )

    result = await reaper.remove_with_cleanup("absent-session", reason="pid_dead")

    assert result is False
    binding_store.remove_binding.assert_not_called()
    auto_approve_service.clear_session.assert_not_awaited()
    hook_socket_server.cancel_pending_permissions.assert_not_awaited()


async def test_remove_with_cleanup_does_not_tombstone_or_invalidate_idle_ttl_removal() -> None:
    binding_store = Mock()
    binding_store.get_binding = Mock(return_value=_make_binding("sess-idle"))
    binding_store.remove_binding = Mock()

    auto_approve_service = Mock()
    auto_approve_service.clear_session = AsyncMock()
    hook_socket_server = Mock()
    hook_socket_server.cancel_pending_permissions = AsyncMock()
    permission_callback_registry = Mock()
    permission_callback_registry.invalidate_session = AsyncMock(return_value=1)
    external_uq_state = Mock()
    external_uq_state.invalidate_session = Mock(return_value=1)
    external_discovery = Mock()
    external_discovery.mark_session_ended = Mock()

    reaper = ExternalBindingReaper(
        binding_store=binding_store,
        auto_approve_service=auto_approve_service,
        hook_socket_server=hook_socket_server,
        permission_callback_registry=permission_callback_registry,
        external_uq_state=external_uq_state,
        external_discovery=external_discovery,
    )

    result = await reaper.remove_with_cleanup("sess-idle", reason="idle_ttl_expired")

    assert result is True
    permission_callback_registry.invalidate_session.assert_not_awaited()
    external_uq_state.invalidate_session.assert_not_called()
    external_discovery.mark_session_ended.assert_not_called()


async def test_remove_with_cleanup_skips_optional_cleanup_when_binding_already_absent() -> None:
    binding_store = Mock()
    binding_store.get_binding = Mock(return_value=None)
    binding_store.remove_binding = Mock()

    auto_approve_service = Mock()
    auto_approve_service.clear_session = AsyncMock()
    hook_socket_server = Mock()
    hook_socket_server.cancel_pending_permissions = AsyncMock()
    permission_callback_registry = Mock()
    permission_callback_registry.invalidate_session = AsyncMock(return_value=1)
    external_uq_state = Mock()
    external_uq_state.invalidate_session = Mock(return_value=1)
    external_discovery = Mock()
    external_discovery.mark_session_ended = Mock()

    reaper = ExternalBindingReaper(
        binding_store=binding_store,
        auto_approve_service=auto_approve_service,
        hook_socket_server=hook_socket_server,
        permission_callback_registry=permission_callback_registry,
        external_uq_state=external_uq_state,
        external_discovery=external_discovery,
    )

    result = await reaper.remove_with_cleanup("absent-session", reason="pid_dead")

    assert result is False
    permission_callback_registry.invalidate_session.assert_not_awaited()
    external_uq_state.invalidate_session.assert_not_called()
    external_discovery.mark_session_ended.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
