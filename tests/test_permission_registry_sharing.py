"""Regression tests: an empty PermissionCallbackRegistry must not be replaced
by a fallback instance just because `__len__` makes it falsy.

History: PermissionCallbackRegistry defines __len__, so an empty (newly
constructed) registry evaluates to False in a boolean context. Several call
sites used `registry or PermissionCallbackRegistry(...)` as a fallback, which
silently replaced a freshly injected empty registry with a brand-new instance.
The result was two registries in the same process: one used by the push
notifier (where tokens were registered) and another used by the
external_permission callback handler (where tokens were resolved). All
callbacks then failed with "ext_perm token resolve failed".

These tests pin the contract: when a real (possibly empty) registry is
provided, the consumer must use that exact instance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.permission_callback_registry import PermissionCallbackRegistry


def test_empty_registry_is_falsy_via_len() -> None:
    """Document the gotcha: an empty registry is falsy because of __len__."""
    registry = PermissionCallbackRegistry(ttl_sec=600)
    assert len(registry) == 0
    assert not registry  # __bool__ falls back to __len__
    assert registry is not None


def test_push_notifier_uses_injected_empty_registry() -> None:
    """ExternalSessionPushNotifier must keep the injected registry, not replace it."""
    from app.services.external_session_push_notifier import ExternalSessionPushNotifier

    bot = MagicMock()
    bot.send_message = AsyncMock()
    binding_store = MagicMock()
    registry = PermissionCallbackRegistry(ttl_sec=600)

    notifier = ExternalSessionPushNotifier(
        bot=bot,
        binding_store=binding_store,
        permission_callback_registry=registry,
    )

    # Must be the SAME instance; an `or` fallback would have created a different one.
    assert notifier._permission_callback_registry is registry


def test_unbound_permission_handler_uses_injected_empty_registry() -> None:
    """UnboundPermissionHandler must keep the injected registry, not replace it."""
    from app.services.unbound_permission_handler import UnboundPermissionHandler

    bot = MagicMock()
    hook_socket_server = MagicMock()
    registry = PermissionCallbackRegistry(ttl_sec=600)

    handler = UnboundPermissionHandler(
        bot=bot,
        hook_socket_server=hook_socket_server,
        allowed_user_ids={1},
        permission_callback_registry=registry,
    )

    assert handler._permission_callback_registry is registry


def test_token_registered_in_one_path_resolves_in_another() -> None:
    """End-to-end-ish: a token registered via the push notifier must resolve
    against the same registry object that the external_permission handler
    receives. Both consumers must share the injected registry."""
    from app.services.external_session_push_notifier import ExternalSessionPushNotifier

    bot = MagicMock()
    bot.send_message = AsyncMock()
    binding_store = MagicMock()
    shared_registry = PermissionCallbackRegistry(ttl_sec=600)

    notifier = ExternalSessionPushNotifier(
        bot=bot,
        binding_store=binding_store,
        permission_callback_registry=shared_registry,
    )

    # The notifier holds a reference to the same registry the caller injected.
    assert notifier._permission_callback_registry is shared_registry

    # Register a token via the registry directly (simulating the path used in
    # notify_permission_request) and confirm it resolves against the injected
    # registry — i.e., the registry truly shared.
    token = shared_registry.register("toolu_abc123")
    assert shared_registry.resolve(token) == "toolu_abc123"


def test_fallback_only_triggers_when_registry_is_none() -> None:
    """If the caller really passes None, a fallback instance is created.

    This documents the intended fallback semantics (used by some unit tests).
    """
    from app.services.external_session_push_notifier import ExternalSessionPushNotifier

    bot = MagicMock()
    binding_store = MagicMock()

    notifier = ExternalSessionPushNotifier(
        bot=bot,
        binding_store=binding_store,
        permission_callback_registry=None,
    )

    assert notifier._permission_callback_registry is not None
    assert isinstance(notifier._permission_callback_registry, PermissionCallbackRegistry)


@pytest.mark.parametrize(
    "factory_args",
    [
        # No explicit kwarg at all (defaults to None internally).
        {},
        # Explicit None.
        {"permission_callback_registry": None},
    ],
)
def test_unbound_handler_fallback_when_no_registry(factory_args: dict) -> None:
    from app.services.unbound_permission_handler import UnboundPermissionHandler

    bot = MagicMock()
    hook_socket_server = MagicMock()

    handler = UnboundPermissionHandler(
        bot=bot,
        hook_socket_server=hook_socket_server,
        allowed_user_ids={1},
        **factory_args,
    )

    assert handler._permission_callback_registry is not None
    assert isinstance(handler._permission_callback_registry, PermissionCallbackRegistry)
