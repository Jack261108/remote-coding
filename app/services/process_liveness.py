"""Local process-liveness probe.

LOCAL_SOCKET_ASSUMPTION: this probe is only valid when the probed pid belongs
to a process on the SAME host as this process. The hook transport is a local
Unix domain socket (asyncio.start_unix_server), so Claude Code and the bot
always share a host and pids are resolvable here via os.kill. If a remote hook
socket is ever introduced, pid liveness becomes meaningless across hosts and
EXTERNAL_BINDING_PID_LIVENESS_ENABLED MUST be set to false (or the mechanism
revised). See Requirement 10.4.
"""

from __future__ import annotations

import os


def process_is_alive(pid: int) -> bool:
    """Return True iff a process with ``pid`` appears to exist locally.

    Pure and synchronous; sends NO signal (uses ``os.kill(pid, 0)``, the
    existence probe). Semantics (Requirement 1):

    - ``pid <= 0``           -> ``False`` (no ``os.kill`` call; not a single-process target)
    - ``os.kill`` returns    -> ``True``
    - ``ProcessLookupError`` -> ``False`` (no such process)
    - ``PermissionError``    -> ``True``  (process exists, owned by another user)
    - any other ``OSError``  -> ``True``  (ambiguous; treat as alive to avoid
      wrongly removing a live session)
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True
