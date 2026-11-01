"""Whether a process, or a whole process group, a test started is gone.

Signal 0 delivers nothing, so asking about any PID passes the session's signal
guard (``tests/_helpers/signal_guard.py``). A killed orphan is reaped by init,
not at once, so the checks that wait for it poll with a bound.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable


def gone(pid: object) -> bool:
    """Whether no process has the PID *pid* (an ``int``)."""
    assert isinstance(pid, int)
    try:
        os.kill(pid, 0)  # signal 0 delivers nothing; it only asks whether the PID exists
    except ProcessLookupError:
        return True
    return False


def groups() -> set[int]:
    """The process group of every process on the machine."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pgid="], capture_output=True, text=True, check=True
    )
    return {int(group) for group in listing.stdout.split()}


async def soon(condition: Callable[[], bool], *, within_s: float = 5.0) -> bool:
    """Whether *condition* holds within *within_s*, polled every 50 ms."""
    for _ in range(int(within_s / 0.05)):
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()
