from __future__ import annotations

from app.services.permission_callback_registry import PermissionCallbackRegistry


def test_registry_resolves_full_tool_use_id_from_short_token() -> None:
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: "abc12345", clock=lambda: 100.0)
    tool_use_id = "toolu_" + "x" * 200

    token = registry.register(tool_use_id)

    assert token == "abc12345"
    assert registry.resolve(token) == tool_use_id
    assert len(token.encode("utf-8")) < len(tool_use_id.encode("utf-8"))


def test_registry_expires_tokens() -> None:
    now = 100.0

    def clock() -> float:
        return now

    registry = PermissionCallbackRegistry(ttl_sec=10, token_factory=lambda: "token001", clock=clock)
    token = registry.register("tool-1")

    now = 111.0

    assert registry.resolve(token) is None


def test_registry_retries_live_token_collision() -> None:
    tokens = iter(["same001", "same001", "next002"])
    registry = PermissionCallbackRegistry(ttl_sec=60, token_factory=lambda: next(tokens), clock=lambda: 100.0)

    first = registry.register("tool-1")
    second = registry.register("tool-2")

    assert first == "same001"
    assert second == "next002"
    assert registry.resolve(first) == "tool-1"
    assert registry.resolve(second) == "tool-2"
