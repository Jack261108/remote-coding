"""LocalProcessProbe test double for external-input service tests."""

from __future__ import annotations

from collections import deque

from app.services.local_process_probe import ProcessTargetReason, ProcessTargetValidation


class FakeLocalProcessProbe:
    def __init__(self, *, tty: str | None = "/dev/ttys005", valid: bool = True) -> None:
        self.tty = tty
        self.valid = valid
        self.reason = ProcessTargetReason.OK if valid else ProcessTargetReason.NOT_FOREGROUND
        self.validation_results: deque[bool] = deque()
        self.validation_calls: list[tuple[int, str]] = []
        self.tty_calls: list[int] = []

    def pid_controlling_tty(self, pid: int) -> str | None:
        self.tty_calls.append(pid)
        return self.tty

    def validate_claude_foreground(
        self,
        *,
        pid: int,
        paired_tty: str,
    ) -> ProcessTargetValidation:
        self.validation_calls.append((pid, paired_tty))
        valid = self.validation_results.popleft() if self.validation_results else self.valid
        return ProcessTargetValidation(
            ok=valid,
            reason=ProcessTargetReason.OK if valid else self.reason,
            tty=paired_tty if valid else self.tty,
            pgid=7,
            foreground_pgid=7 if valid else 8,
            command="claude" if valid else "zsh",
        )
