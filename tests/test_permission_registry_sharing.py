"""Regression tests for Phase 7 legacy registry constructor cleanup."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from app.services.permission_callback_registry import PermissionCallbackRegistry


def test_registry_no_longer_exposes_legacy_len_bool_contract() -> None:
    registry = PermissionCallbackRegistry(ttl_sec=600)

    assert not hasattr(registry, "__len__")
    assert bool(registry) is True


def test_push_notifier_no_longer_accepts_legacy_registry_kwarg() -> None:
    from app.services.external_session_push_notifier import ExternalSessionPushNotifier

    signature = inspect.signature(ExternalSessionPushNotifier)

    assert "permission_callback_registry" not in signature.parameters


def test_unbound_permission_handler_no_longer_accepts_legacy_registry_kwarg() -> None:
    from app.services.unbound_permission_handler import UnboundPermissionHandler

    signature = inspect.signature(UnboundPermissionHandler)

    assert "permission_callback_registry" not in signature.parameters


def test_push_notifier_does_not_keep_legacy_registry_field() -> None:
    from app.services.external_session_push_notifier import ExternalSessionPushNotifier

    notifier = ExternalSessionPushNotifier(message_sender=MagicMock(), binding_store=MagicMock())

    assert not hasattr(notifier, "_permission_callback_registry")


def test_unbound_permission_handler_does_not_keep_legacy_registry_field() -> None:
    from app.services.unbound_permission_handler import UnboundPermissionHandler

    handler = UnboundPermissionHandler(bot=MagicMock(), hook_socket_server=MagicMock(), allowed_user_ids={1})

    assert not hasattr(handler, "_permission_callback_registry")
