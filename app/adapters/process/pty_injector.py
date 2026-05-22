"""PTY/TTY injection for external Claude sessions.

Finds the tmux pane containing an external Claude process and injects
keystrokes via `tmux send-keys`. This enables fully automated Telegram-driven
answers to AskUserQuestion prompts in external sessions.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

logger = logging.getLogger(__name__)

_TMUX_BIN = "tmux"


async def find_tmux_pane_for_pid(pid: int) -> str | None:
    """Walk the process tree from *pid* upward, looking for a tmux pane whose
    shell PID matches an ancestor. Returns the pane ID (e.g. ``%3``) or None.
    """
    tmux_bin = shutil.which(_TMUX_BIN)
    if tmux_bin is None:
        return None

    # Get all tmux panes and their shell PIDs
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux_bin,
            "list-panes",
            "-a",
            "-F",
            "#{pane_id} #{pane_pid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
    except (FileNotFoundError, OSError):
        return None

    pane_pids: dict[int, str] = {}
    for line in stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                pane_pids[int(parts[1])] = parts[0]
            except ValueError:
                continue

    if not pane_pids:
        return None

    # Walk up the process tree from pid
    current = pid
    visited: set[int] = set()
    for _ in range(30):  # max depth
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


async def inject_keys_via_tmux(pane_id: str, *keys: str) -> tuple[bool, str]:
    """Send keystrokes to a tmux pane via ``tmux send-keys``."""
    tmux_bin = shutil.which(_TMUX_BIN)
    if tmux_bin is None:
        return False, "tmux not found"
    if not keys:
        return True, ""
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux_bin,
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
) -> tuple[bool, str]:
    """Select an option in the Claude TUI by moving cursor down and pressing Enter.

    Assumes cursor starts at the first option (index 0). Sends Down arrow
    *option_index* times, then Enter.
    """
    # Move cursor to the target option
    if option_index > 0:
        for _ in range(option_index):
            ok, err = await inject_keys_via_tmux(pane_id, "Down")
            if not ok:
                return False, err
            await asyncio.sleep(0.05)

    # Select the option
    ok, err = await inject_keys_via_tmux(pane_id, "C-m")
    if not ok:
        return False, err

    # Submit if this is the final question
    if submit_after:
        await asyncio.sleep(enter_delay_sec)
        ok, err = await inject_keys_via_tmux(pane_id, "C-m")
        if not ok:
            return False, err

    return True, ""


async def inject_text_answer(
    pane_id: str,
    *,
    text: str,
    option_count: int,
    submit_after: bool = False,
    enter_delay_sec: float = 0.15,
) -> tuple[bool, str]:
    """Navigate past options to the text input field, type text, and submit.

    Moves cursor down past all options to reach "Other (type answer)", selects it,
    then types the text.
    """
    # Move to "Other" option (after all regular options)
    for _ in range(option_count):
        ok, err = await inject_keys_via_tmux(pane_id, "Down")
        if not ok:
            return False, err
        await asyncio.sleep(0.05)

    # Select "Other"
    ok, err = await inject_keys_via_tmux(pane_id, "C-m")
    if not ok:
        return False, err
    await asyncio.sleep(enter_delay_sec)

    # Type the text (use tmux send-keys with literal text)
    # Escape special characters for tmux
    ok, err = await inject_keys_via_tmux(pane_id, text)
    if not ok:
        return False, err

    # Submit
    ok, err = await inject_keys_via_tmux(pane_id, "C-m")
    if not ok:
        return False, err

    if submit_after:
        await asyncio.sleep(enter_delay_sec)
        ok, err = await inject_keys_via_tmux(pane_id, "C-m")
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
