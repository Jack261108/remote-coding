"""Unit tests for LocalProcessProbe.

Covers every ``ProcessTargetReason`` reachable from the aggregated
``validate_claude_foreground`` check, using injected resolvers so no real
process table, PTY, or ``ps`` is required. The aggregated validator is the
trust anchor for external Ghostty input injection: every failure fails closed
(``ok=False`` with a distinct reason) so the service never injects into a
shell that Enter would execute.
"""

from __future__ import annotations

from app.services.local_process_probe import (
    LocalProcessProbe,
    ProcessCommandSignature,
    ProcessTargetReason,
    _looks_like_claude,
)


def _probe(
    *,
    alive: bool = True,
    tty: tuple[str | None, str | None] = ("/dev/ttys005", None),
    pgid: int | None = 7,
    foreground: int | None = 7,
    command: str | None = "claude",
    process_state: str = "S+",
) -> LocalProcessProbe:
    signature = None
    if command is not None:
        comm = command.split(maxsplit=1)[0]
        signature = ProcessCommandSignature(state=process_state, comm=comm, args=command)
    return LocalProcessProbe(
        pid_is_alive=lambda _pid: alive,
        tty_resolver=lambda _pid: tty,
        pgid_resolver=lambda _pid: pgid,
        foreground_resolver=lambda _tty: foreground,
        command_resolver=lambda _pid: signature,
    )


def _signature(
    comm: str,
    args: str | None = None,
    *,
    state: str = "S+",
) -> ProcessCommandSignature:
    return ProcessCommandSignature(state=state, comm=comm, args=args or comm)


# --- aggregated validator: every reason ------------------------------------


def test_validate_ok_when_foreground_claude_on_paired_tty() -> None:
    result = _probe().validate_claude_foreground(pid=1234, paired_tty="/dev/ttys005")
    assert result.ok and result.reason == ProcessTargetReason.OK
    assert result.tty == "/dev/ttys005"
    assert result.pgid == 7 and result.foreground_pgid == 7


def test_validate_pid_not_positive() -> None:
    result = _probe().validate_claude_foreground(pid=0, paired_tty="/dev/ttys005")
    assert not result.ok and result.reason == ProcessTargetReason.PID_NOT_POSITIVE


def test_validate_pid_dead() -> None:
    result = _probe(alive=False).validate_claude_foreground(pid=1234, paired_tty="/dev/ttys005")
    assert not result.ok and result.reason == ProcessTargetReason.PID_DEAD


def test_validate_zombie_and_unknown_state_fail_closed() -> None:
    zombie = _probe(process_state="Z+").validate_claude_foreground(
        pid=1234,
        paired_tty="/dev/ttys005",
    )
    assert not zombie.ok and zombie.reason == ProcessTargetReason.PID_ZOMBIE

    unknown = _probe(process_state="?").validate_claude_foreground(
        pid=1234,
        paired_tty="/dev/ttys005",
    )
    assert not unknown.ok and unknown.reason == ProcessTargetReason.COMMAND_UNKNOWN


def test_validate_tty_unresolved() -> None:
    result = _probe(tty=(None, ProcessTargetReason.TTY_UNRESOLVED)).validate_claude_foreground(pid=1234, paired_tty="/dev/ttys005")
    assert not result.ok and result.reason == ProcessTargetReason.TTY_UNRESOLVED


def test_validate_tty_mismatch_blocks_injection() -> None:
    """A pid that moved to a different tty MUST be refused so we never inject
    into a PTY that is no longer Claude's (could now be a different program)."""
    result = _probe(tty=("/dev/ttys009", None)).validate_claude_foreground(pid=1234, paired_tty="/dev/ttys005")
    assert not result.ok and result.reason == ProcessTargetReason.TTY_MISMATCH
    assert result.tty == "/dev/ttys009"


def test_validate_tty_normalises_paired_without_dev_prefix() -> None:
    """paired_tty may be stored without /dev prefix in some legacy feeds; the
    comparison normalises both sides, so a bare ``ttys005`` still matches."""
    result = _probe().validate_claude_foreground(pid=1234, paired_tty="ttys005")
    assert result.ok and result.reason == ProcessTargetReason.OK


def test_validate_foreground_unknown_fails_closed() -> None:
    """If tcgetpgrp cannot be read (e.g. tty fd closed/EACCES) we refuse rather
    than guessing — fail-closed is the security contract."""
    result = _probe(foreground=None).validate_claude_foreground(pid=1234, paired_tty="/dev/ttys005")
    assert not result.ok and result.reason == ProcessTargetReason.FOREGROUND_UNKNOWN
    assert result.pgid == 7


def test_validate_not_foreground_refuses_shell_takeover() -> None:
    """If Claude Ctrl-C'd back to a shell, the shell is foreground (pgid
    differs) — refuse so the typed text never reaches the shell."""
    result = _probe(foreground=999).validate_claude_foreground(pid=1234, paired_tty="/dev/ttys005")
    assert not result.ok and result.reason == ProcessTargetReason.NOT_FOREGROUND
    assert result.foreground_pgid == 999


def test_validate_pgroup_unknown() -> None:
    result = _probe(pgid=None).validate_claude_foreground(pid=1234, paired_tty="/dev/ttys005")
    assert not result.ok and result.reason == ProcessTargetReason.PGROUP_UNKNOWN


def test_validate_command_unknown_fails_closed() -> None:
    """An unresolvable command identity is no positive proof — refuse."""
    result = _probe(command=None).validate_claude_foreground(pid=1234, paired_tty="/dev/ttys005")
    assert not result.ok and result.reason == ProcessTargetReason.COMMAND_UNKNOWN


def test_validate_obvious_shell_command_refused() -> None:
    """The foreground pgroup matches, but ps comm is ``bash``: Claude exited
    while a shell re-took the pgroup numerically — refuse."""
    result = _probe(command="bash").validate_claude_foreground(pid=1234, paired_tty="/dev/ttys005")
    assert not result.ok and result.reason == ProcessTargetReason.NOT_CLAUDE
    assert result.command == "bash"


def test_validate_node_claude_path_is_accepted_but_plain_node_is_not() -> None:
    """Older installs run under node, so args must contain positive Claude
    identity; plain node is not sufficient proof."""
    result = _probe(command="node /opt/@anthropic-ai/claude-code/cli.js").validate_claude_foreground(
        pid=1234,
        paired_tty="/dev/ttys005",
    )
    assert result.ok and result.reason == ProcessTargetReason.OK

    plain = _probe(command="node").validate_claude_foreground(
        pid=1234,
        paired_tty="/dev/ttys005",
    )
    assert not plain.ok and plain.reason == ProcessTargetReason.NOT_CLAUDE


# --- primitive accessors ----------------------------------------------------


def test_pid_controlling_tty_returns_resolved_value() -> None:
    probe = _probe(tty=("/dev/ttys123", None))
    assert probe.pid_controlling_tty(123) == "/dev/ttys123"


def test_pid_process_group_id_returns_resolved_value() -> None:
    probe = _probe(pgid=42)
    assert probe.pid_process_group_id(123) == 42


def test_tty_foreground_pgroup_returns_resolved_value() -> None:
    probe = _probe(foreground=42)
    assert probe.tty_foreground_pgroup("/dev/ttys1") == 42


def test_pid_command_signature_returns_structured_snapshot() -> None:
    probe = _probe(command="claude --resume abc", process_state="S+")
    assert probe.pid_command_signature(123) == ProcessCommandSignature(
        state="S+",
        comm="claude",
        args="claude --resume abc",
    )


# --- _looks_like_claude unit guard ------------------------------------------


def test_looks_like_claude_rejects_shells_and_interpreter_argv_spoofing() -> None:
    for shell in ("bash", "zsh", "sh", "fish", "tcsh", "csh", "dash", "ksh"):
        assert not _looks_like_claude(_signature(shell, f"{shell} -lc claude"))
    assert not _looks_like_claude(_signature("python", "python /tmp/claude-code/cli.py"))
    assert not _looks_like_claude(_signature("node", "node /tmp/claude-code/cli.js"))
    assert not _looks_like_claude(_signature("node"))
    assert not _looks_like_claude(_signature("notclaude"))


def test_looks_like_claude_accepts_native_and_official_node_entry() -> None:
    assert _looks_like_claude(_signature("claude", "claude --resume abc"))
    assert _looks_like_claude(_signature("/opt/bin/claude-code"))
    assert _looks_like_claude(
        _signature(
            "node",
            "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
        )
    )
    assert not _looks_like_claude(None), "None is no positive proof -> False"
