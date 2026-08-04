"""Ghostty adapter test double used by external-input service tests."""

from __future__ import annotations

import asyncio
from collections import deque

from app.adapters.process.ghostty_terminal_adapter import GhosttyTerminal, InjectionOutcome


class FakeGhosttyTerminalAdapter:
    def __init__(
        self,
        *,
        available: bool = True,
        terminals: list[GhosttyTerminal] | None = None,
    ) -> None:
        self.available = available
        self.terminals = (
            terminals
            if terminals is not None
            else [
                GhosttyTerminal(
                    terminal_id="term-1",
                    name="claude — project",
                    cwd="/project",
                )
            ]
        )
        self.list_error: str | None = None
        self.validate_error: str | None = None
        self.inject_outcomes: deque[str] = deque()
        self.inject_calls: list[tuple[str, str]] = []
        self.validate_calls: list[str] = []
        self.validate_entered: asyncio.Event | None = None
        self.validate_release: asyncio.Event | None = None
        self.inject_entered: asyncio.Event | None = None
        self.inject_release: asyncio.Event | None = None
        self.active_injections = 0
        self.max_active_injections = 0

    def is_available(self) -> bool:
        return self.available

    async def list_terminals(self) -> tuple[list[GhosttyTerminal] | None, str | None]:
        if not self.available:
            return None, "unavailable"
        if self.list_error is not None:
            return None, self.list_error
        return list(self.terminals), None

    async def validate_terminal(
        self,
        terminal_id: str,
    ) -> tuple[bool, GhosttyTerminal | None, str | None]:
        self.validate_calls.append(terminal_id)
        if self.validate_entered is not None:
            self.validate_entered.set()
        if self.validate_release is not None:
            await self.validate_release.wait()
        if not self.available:
            return False, None, "unavailable"
        if self.validate_error is not None:
            return False, None, self.validate_error
        matches = [terminal for terminal in self.terminals if terminal.terminal_id == terminal_id]
        if len(matches) == 1:
            return True, matches[0], None
        if not matches:
            return False, None, InjectionOutcome.NOT_FOUND
        return False, None, InjectionOutcome.NOT_UNIQUE

    async def inject_text(self, terminal_id: str, text: str) -> str:
        self.inject_calls.append((terminal_id, text))
        self.active_injections += 1
        self.max_active_injections = max(self.max_active_injections, self.active_injections)
        try:
            if self.inject_entered is not None:
                self.inject_entered.set()
            if self.inject_release is not None:
                await self.inject_release.wait()
            return self.inject_outcomes.popleft() if self.inject_outcomes else InjectionOutcome.OK
        finally:
            self.active_injections -= 1
