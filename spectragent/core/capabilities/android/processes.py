"""The processes the Android capability starts, and how it stops them.

A run starts two: its private adb server and, with the local emulator
provider, the emulator. Each leads a process group of its own
(``process_group=0``), like the browser's Chrome
(``docs/architecture_design.md`` §4.9.x): a terminal's Ctrl-C then reaches this
process only, and its teardown stops them in order — the emulator before the
adb server the emulator's own adb calls go to. Only a process started here is
signalled, through its own handle; its group is signalled only right after its
leader was seen to exit, when what is left of the group (the emulator's
``netsimd`` and adb clients) can only be its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

#: How long a process gets to exit after SIGKILL before it is reported as stuck.
KILL_WAIT_S = 5.0


async def spawn(
    argv: Sequence[str], *, env: Mapping[str, str], log: Path
) -> asyncio.subprocess.Process:
    """Start *argv* leading a process group of its own, its output appended to *log*.

    Cancelling ``create_subprocess_exec`` can lose the process after the fork,
    so a cancelled start finishes acquiring the handle, stops the process, and
    then re-raises the cancellation.
    """
    with log.open("ab") as output:
        starting = asyncio.ensure_future(
            asyncio.create_subprocess_exec(
                *argv,
                env=dict(env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
                process_group=0,
            )
        )
        try:
            return await asyncio.shield(starting)
        except asyncio.CancelledError as cancelled:
            try:
                process = await settle(starting)
            except Exception:
                raise cancelled from None  # it never started
            try:
                await settle(asyncio.ensure_future(stop(process, grace_s=0.0)))
            except Exception as exc:
                cancelled.add_note(f"stopping the process it had started failed: {exc}")
            raise


async def stop(process: asyncio.subprocess.Process, *, grace_s: float) -> None:
    """SIGTERM *process*, SIGKILL it after *grace_s*, then what is left of its group.

    A process that had already exited before this call is left alone, group
    included: it exited at a moment nobody saw, and its group ID may belong to
    another process by now.

    Raises:
        RuntimeError: It was still running :data:`KILL_WAIT_S` after SIGKILL.
    """
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=KILL_WAIT_S)
        except TimeoutError:
            raise RuntimeError(
                f"process {process.pid} was still running {KILL_WAIT_S:g} s after SIGKILL"
            ) from None
    # Right after its leader exited: a group with members left keeps its ID,
    # so the signal reaches this process's own children or nothing. EPERM is
    # what macOS answers for a group of zombies only; both mean nothing is left.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


async def settle(task: asyncio.Future[T]) -> T:
    """Await *task* to its end even if the caller is cancelled meanwhile, then return its result.

    For cleanup that must not be abandoned halfway: a second cancellation of
    the caller is absorbed until the task is done.
    """
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    return task.result()


def log_tail(log: Path, *, lines: int = 20) -> str:
    """The last *lines* of *log*, for an error message; empty when it cannot be read."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
