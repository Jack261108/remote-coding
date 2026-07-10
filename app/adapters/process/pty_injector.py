"""PTY/TTY injection for external Claude sessions.

Finds the tmux pane containing an external Claude process and injects
keystrokes via `tmux send-keys`. This enables fully automated Telegram-driven
answers to AskUserQuestion prompts in external sessions.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DEFAULT_TMUX_BIN = "tmux"


@dataclass(frozen=True, slots=True)
class _TmuxTarget:
    pane_id: str
    session_name: str


async def _find_tmux_target_for_pid(pid: int, tmux_bin: str) -> _TmuxTarget | None:
    resolved = shutil.which(tmux_bin)
    if resolved is None:
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            resolved,
            "list-panes",
            "-a",
            "-F",
            "#{session_name} #{pane_id} #{pane_pid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
    except (FileNotFoundError, OSError):
        return None

    pane_pids: dict[int, _TmuxTarget] = {}
    for line in stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        session_name, pane_id, pane_pid = parts
        try:
            pane_pids[int(pane_pid)] = _TmuxTarget(
                pane_id=pane_id,
                session_name=session_name,
            )
        except ValueError:
            continue

    if not pane_pids:
        return None

    current = pid
    visited: set[int] = set()
    for _ in range(30):
        if current in pane_pids:
            return pane_pids[current]
        if current in visited or current <= 1:
            break
        visited.add(current)
        parent = await _get_ppid(current)
        if parent is None or parent <= 1 or parent == current:
            break
        current = parent

    return None


async def find_tmux_pane_for_pid(pid: int, tmux_bin: str = _DEFAULT_TMUX_BIN) -> str | None:
    """Return the tmux pane ID containing *pid* or one of its ancestors."""
    target = await _find_tmux_target_for_pid(pid, tmux_bin)
    return target.pane_id if target is not None else None


async def find_tmux_session_for_pid(pid: int, tmux_bin: str = _DEFAULT_TMUX_BIN) -> str | None:
    """Return the tmux session name containing *pid* or one of its ancestors."""
    target = await _find_tmux_target_for_pid(pid, tmux_bin)
    return target.session_name if target is not None else None


async def inject_keys_via_tmux(pane_id: str, *keys: str, tmux_bin: str = _DEFAULT_TMUX_BIN) -> tuple[bool, str]:
    """Send keystrokes to a tmux pane via ``tmux send-keys``."""
    resolved = shutil.which(tmux_bin)
    if resolved is None:
        return False, "tmux not found"
    if not keys:
        return True, ""
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved,
            "send-keys",
            "-t",
            pane_id,
            *keys,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, ""
        err = stderr.decode(errors="replace").strip() or "unknown error"
        return False, f"tmux send-keys failed: {err}"
    except (FileNotFoundError, OSError) as exc:
        return False, f"tmux send-keys error: {exc}"


async def inject_option_selection(
    pane_id: str,
    *,
    option_index: int,
    submit_after: bool = False,
    enter_delay_sec: float = 0.15,
    tmux_bin: str = _DEFAULT_TMUX_BIN,
) -> tuple[bool, str]:
    """Select an option in the Claude TUI by moving cursor down and pressing Enter.

    Assumes cursor starts at the first option (index 0). Sends Down arrow
    *option_index* times, then Enter.
    """
    resolved = shutil.which(tmux_bin)
    if resolved is None:
        return False, "tmux not found"
    if option_index > 0:
        for _ in range(option_index):
            ok, err = await inject_keys_via_tmux(pane_id, "Down", tmux_bin=resolved)
            if not ok:
                return False, err
            await asyncio.sleep(0.05)

    ok, err = await inject_keys_via_tmux(pane_id, "C-m", tmux_bin=resolved)
    if not ok:
        return False, err

    if submit_after:
        await asyncio.sleep(enter_delay_sec)
        ok, err = await inject_keys_via_tmux(pane_id, "C-m", tmux_bin=resolved)
        if not ok:
            return False, err

    return True, ""


async def _get_ppid(pid: int) -> int | None:
    """Get parent PID of a process."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-p",
            str(pid),
            "-o",
            "ppid=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        text = stdout.decode(errors="replace").strip()
        if text and text.isdigit():
            return int(text)
        return None
    except (FileNotFoundError, OSError):
        return None
