"""Local process / TTY probe for external Ghostty session input.

Verifies, immediately before each injection, that the bound Claude process is
still the foreground program of the same PTY it was paired on. PID alone is not
a stable terminal identity (it can be recycled after Claude exits), so the
trust anchor is the ``(pid, paired_tty)`` pair checked together:

  1. ``pid`` is alive and positive (reuses ``process_liveness.process_is_alive``);
  2. ``pid`` is still the foreground process group of ``paired_tty`` — i.e.
     ``os.tcgetpgrp(tty_fd) == os.getpgid(pid)``. This is the only check that
     proves "Claude is the program that would receive the keystrokes right
     now". If the user has ``Ctrl-C``'d back to a shell, the shell is foreground
     and this check fails — we refuse so the typed text is never handed to a
     shell that Enter would execute.
  3. The command identity of the foreground process still looks like Claude
     (not a generic shell). This guards against Claude having exited while a
     long-lived parent shell re-took the same foreground pgroup id by mere
     numerical coincidence.

LOCAL_SOCKET_ASSUMPTION (inherited from ``process_liveness``): pids and ttys
are only meaningful on the same host as this process. The bot and Claude Code
share a host by virtue of the local hook Unix socket; if that ever changes this
probe MUST be disabled too. See ``process_liveness.py``.

All raw OS operations (``ps``, ``os.tcgetpgrp``, ``os.getpgid``) are tiny
dependency-injected functions so tests run without a real PTY or process table.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.services.process_liveness import process_is_alive

logger = logging.getLogger(__name__)


class ProcessTargetReason:
    """Stable reason constants for ``ProcessTargetValidation.reason``.

    Plain strings (not an enum) so callers can compare without importing the
    enum. Keep stable.
    """

    OK = "ok"
    PID_NOT_POSITIVE = "pid_not_positive"
    PID_DEAD = "pid_dead"
    PID_ZOMBIE = "pid_zombie"
    TTY_UNRESOLVED = "tty_unresolved"
    TTY_MISMATCH = "tty_mismatch"
    FOREGROUND_UNKNOWN = "foreground_unknown"
    NOT_FOREGROUND = "not_foreground"
    PGROUP_UNKNOWN = "pgroup_unknown"
    COMMAND_UNKNOWN = "command_unknown"
    NOT_CLAUDE = "not_claude"


@dataclass(frozen=True, slots=True)
class ProcessCommandSignature:
    """One ``ps`` snapshot used for strict process identity/liveness checks."""

    state: str
    comm: str
    args: str

    @property
    def display(self) -> str:
        return self.args


@dataclass(frozen=True, slots=True)
class ProcessTargetValidation:
    """Result of validating a bound Claude process before injection.

    ``ok`` is True iff every check passed and it is safe to inject. ``reason``
    is one of the ``ProcessTargetReason`` constants. ``tty``/``pgid``/``command``
    are the resolved values (None when unknown) for diagnostics and tests.
    """

    ok: bool
    reason: str
    tty: str | None = None
    pgid: int | None = None
    foreground_pgid: int | None = None
    command: str | None = None


# --- raw probes (dependency-injected; default impls below) --------------------


# A resolver returns (tty, None) on success, (None, reason) when the PID has no
# controlling tty or the lookup failed. ``reason`` is a ProcessTargetReason
# constant (TTY_UNRESOLVED for "pid dead or ps failed").
TtyResolver = Callable[[int], tuple[str | None, str | None]]
PgidResolver = Callable[[int], int | None]
# Returns the foreground pgroup of an open tty path, or None on any error.
ForegroundResolver = Callable[[str], int | None]
# Returns a single process state/comm/args snapshot, or None on lookup failure.
CommandResolver = Callable[[int], ProcessCommandSignature | None]


def _default_pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _default_foreground(tty: str) -> int | None:
    """Return the foreground process group of ``tty`` via ``os.tcgetpgrp``.

    Opens ``tty`` O_RDWR | O_NOCTTY. A failure to open or to read the
    foreground pgroup is treated as unknown (None) — the caller then refuses the
    injection rather than guessing.
    """
    try:
        fd = os.open(tty, os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None
    try:
        return os.tcgetpgrp(fd)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - close best-effort
            pass


def _default_command(pid: int) -> ProcessCommandSignature | None:
    """Return one strict ``ps`` snapshot: state, executable name and argv.

    ``stat/ucomm/command`` are emitted by one ``ps`` invocation, avoiding a
    PID-reuse gap between separate state and identity queries. ``ucomm`` is a
    whitespace-free executable name on macOS, so ``split(maxsplit=2)`` leaves
    the full argv intact. Zombie/unknown state is rejected by the validator.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat=", "-o", "ucomm=", "-o", "command="],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.decode(errors="replace").strip().split(maxsplit=2)
    if len(parts) != 3:
        return None
    state, comm, args = parts
    return ProcessCommandSignature(state=state, comm=comm, args=args)


def _default_tty(pid: int) -> tuple[str | None, str | None]:
    """Return ``(tty, None)`` or ``(None, reason)`` for ``pid`` via ``ps``.

    Synchronous ``subprocess.run`` so it is loop-agnostic. ``ps -p <pid> -o
    tt=`` prints the controlling tty name (e.g. ``ttys005``) or ``??`` when the
    process has no controlling tty. We normalise to ``/dev/<name>`` to match
    the binding's ``paired_tty``, which we always store as an absolute /dev path.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "tt="],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None, ProcessTargetReason.TTY_UNRESOLVED
    text = proc.stdout.decode(errors="replace").strip()
    if proc.returncode != 0 or not text or text == "??":
        return None, ProcessTargetReason.TTY_UNRESOLVED
    return (text if text.startswith("/dev/") else f"/dev/{text}"), None


_LIVE_PROCESS_STATES = frozenset({"R", "S", "D", "I", "U", "W"})
_NATIVE_CLAUDE_COMMANDS = frozenset({"claude", "claude-code"})
_NODE_COMMANDS = frozenset({"node", "nodejs"})
_OFFICIAL_NODE_ENTRY_RE = re.compile(
    r"(?:^|[/\\])@anthropic-ai[/\\]claude-code(?:[/\\]|$)",
    re.IGNORECASE,
)


def _looks_like_claude(signature: ProcessCommandSignature | None) -> bool:
    """Require positive executable identity, never a free-form argv substring.

    Accepted forms:
      * native executable basename exactly ``claude`` / ``claude-code``;
      * ``node`` / ``nodejs`` whose argv contains the official scoped npm
        package path ``@anthropic-ai/claude-code``.

    A shell, Python, arbitrary interpreter, plain node process, or a path that
    merely contains ``claude`` is refused.
    """
    if signature is None:
        return False
    comm = os.path.basename(signature.comm).lower()
    if comm in _NATIVE_CLAUDE_COMMANDS:
        return True
    if comm in _NODE_COMMANDS:
        return bool(_OFFICIAL_NODE_ENTRY_RE.search(signature.args))
    return False


class LocalProcessProbe:
    """Validates a bound Claude PID/TTY before external input injection.

    Construct with custom resolvers in tests; defaults talk to ``ps`` and ``os``.
    ``pid_is_alive`` defaults to ``process_liveness.process_is_alive``.
    """

    def __init__(
        self,
        *,
        pid_is_alive: Callable[[int], bool] = process_is_alive,
        tty_resolver: TtyResolver | None = None,
        pgid_resolver: PgidResolver | None = None,
        foreground_resolver: ForegroundResolver | None = None,
        command_resolver: CommandResolver | None = None,
    ) -> None:
        self._pid_is_alive = pid_is_alive
        self._tty_resolver = tty_resolver or _default_tty
        self._pgid_resolver = pgid_resolver or _default_pgid
        self._foreground_resolver = foreground_resolver or _default_foreground
        self._command_resolver = command_resolver or _default_command

    # --- primitives (exposed for the service & tests) -----------------------

    def pid_controlling_tty(self, pid: int) -> str | None:
        """Return the controlling tty of ``pid`` (normalised ``/dev/...``) or None."""
        tty, _ = self._tty_resolver(pid)
        return tty

    def pid_process_group_id(self, pid: int) -> int | None:
        """Return the process group id of ``pid`` or None if it cannot be resolved."""
        return self._pgid_resolver(pid)

    def tty_foreground_pgroup(self, tty: str) -> int | None:
        """Return the current foreground process group of ``tty`` or None."""
        return self._foreground_resolver(tty)

    def pid_command_signature(self, pid: int) -> ProcessCommandSignature | None:
        """Return a normalised state/comm/args snapshot for ``pid`` or None."""
        signature = self._command_resolver(pid)
        if signature is None:
            return None
        state = signature.state.strip()
        comm = signature.comm.strip()
        args = signature.args.strip()
        if not state or not comm or not args:
            return None
        return ProcessCommandSignature(state=state, comm=comm, args=args)

    # --- aggregated validation ---------------------------------------------

    def validate_claude_foreground(self, *, pid: int, paired_tty: str) -> ProcessTargetValidation:
        """Validate that ``pid`` is still the foreground Claude on ``paired_tty``.

        Returns a ``ProcessTargetValidation``; ``ok`` is True only when ALL of:
        pid positive & alive, controlling tty == paired_tty, foreground pgroup
        of the tty == pgid of pid, and the foreground command still looks like
        Claude (not an obvious shell). Any unknown check fails closed (``ok``
        False) with a distinct reason.
        """
        if pid <= 0:
            return ProcessTargetValidation(ok=False, reason=ProcessTargetReason.PID_NOT_POSITIVE)
        if not self._pid_is_alive(pid):
            return ProcessTargetValidation(ok=False, reason=ProcessTargetReason.PID_DEAD)

        # Controlling tty of the pid.
        tty, tty_err = self._tty_resolver(pid)
        if tty is None:
            return ProcessTargetValidation(ok=False, reason=tty_err or ProcessTargetReason.TTY_UNRESOLVED)
        # Normalise paired_tty for comparison (some sources omit the /dev prefix).
        paired = paired_tty if paired_tty.startswith("/dev/") else f"/dev/{paired_tty}"
        if tty != paired:
            return ProcessTargetValidation(
                ok=False,
                reason=ProcessTargetReason.TTY_MISMATCH,
                tty=tty,
            )

        pgid = self._pgid_resolver(pid)
        if pgid is None:
            return ProcessTargetValidation(
                ok=False,
                reason=ProcessTargetReason.PGROUP_UNKNOWN,
                tty=tty,
            )

        foreground = self._foreground_resolver(paired)
        if foreground is None:
            return ProcessTargetValidation(
                ok=False,
                reason=ProcessTargetReason.FOREGROUND_UNKNOWN,
                tty=tty,
                pgid=pgid,
            )
        if foreground != pgid:
            return ProcessTargetValidation(
                ok=False,
                reason=ProcessTargetReason.NOT_FOREGROUND,
                tty=tty,
                pgid=pgid,
                foreground_pgid=foreground,
            )

        signature = self.pid_command_signature(pid)
        if signature is None:
            # Unknown state/identity is no positive proof.
            return ProcessTargetValidation(
                ok=False,
                reason=ProcessTargetReason.COMMAND_UNKNOWN,
                tty=tty,
                pgid=pgid,
                foreground_pgid=foreground,
            )
        state_code = signature.state[0].upper()
        if state_code == "Z":
            return ProcessTargetValidation(
                ok=False,
                reason=ProcessTargetReason.PID_ZOMBIE,
                tty=tty,
                pgid=pgid,
                foreground_pgid=foreground,
                command=signature.display,
            )
        if state_code not in _LIVE_PROCESS_STATES:
            return ProcessTargetValidation(
                ok=False,
                reason=ProcessTargetReason.COMMAND_UNKNOWN,
                tty=tty,
                pgid=pgid,
                foreground_pgid=foreground,
                command=signature.display,
            )
        if not _looks_like_claude(signature):
            return ProcessTargetValidation(
                ok=False,
                reason=ProcessTargetReason.NOT_CLAUDE,
                tty=tty,
                pgid=pgid,
                foreground_pgid=foreground,
                command=signature.display,
            )

        return ProcessTargetValidation(
            ok=True,
            reason=ProcessTargetReason.OK,
            tty=tty,
            pgid=pgid,
            foreground_pgid=foreground,
            command=signature.display,
        )
