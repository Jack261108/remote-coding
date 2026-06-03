"""Backward-compatible re-export. Canonical location: app.infra.lock_registry."""

from app.infra.lock_registry import RefCountedLockRegistry

__all__ = ["RefCountedLockRegistry"]
