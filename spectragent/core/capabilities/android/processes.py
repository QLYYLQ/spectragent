"""The processes the Android capability starts, how they are tied to this one, and how they stop.

A run starts two programs: its private adb server and, with the local emulator
provider, the emulator. Each runs in a process group of its own
(:class:`ProcessGroup`), opened by a watcher, ``sh -c 'read _; kill -KILL 0'``,
whose stdin is a pipe whose writing end only this Python process holds. The
watcher leads the group and the program joins it (``docs/architecture_design.md``
§11.2):

- **Tied to this process.** However this process ends, SIGKILL included, the
  kernel closes the writing end, the watcher's ``read`` returns and it kills
  its whole group, itself included: an emulator never outlives the run that
  started it.
- **The group's ID stays ours.** While the watcher lives, the group has a
  member, so its ID cannot pass to another process; teardown can therefore
  always signal the group, even after the program that joined it exited.
- **In order.** A terminal's Ctrl-C reaches this process only, and its teardown
  stops each program (SIGTERM, then SIGKILL after a grace period) and then
  everything left in its group, the emulator's ``netsimd`` and adb clients
  among them: the emulator before the adb server its own adb calls go to.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

#: How long a process gets to exit after SIGKILL before it is reported as stuck.
KILL_WAIT_S = 5.0
#: Waits for EOF on its stdin, then kills its whole process group, itself included.
_WATCHER = ("/bin/sh", "-c", "read _; kill -KILL 0")


@dataclass
class ProcessGroup:
    """A process group this run owns, led by a watcher tied to this Python process."""

    watcher: asyncio.subprocess.Process
    #: The writing end of the watcher's stdin; closing it, or this process ending, ends the group.
    _tether: int | None = field(repr=False)

    @property
    def id(self) -> int:
        return self.watcher.pid

    @classmethod
    async def open(cls) -> ProcessGroup:
        """Start a watcher leading a new process group; programs join it with :meth:`spawn`."""
        reading, writing = os.pipe()  # not inherited by any other child (PEP 446)
        try:
            watcher = await asyncio.create_subprocess_exec(
                *_WATCHER,
                stdin=reading,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                process_group=0,
            )
        except BaseException:
            os.close(writing)
            raise
        finally:
            os.close(reading)
        return cls(watcher, writing)

    async def spawn(
        self, argv: Sequence[str], *, env: Mapping[str, str], log: Path
    ) -> asyncio.subprocess.Process:
        """Start *argv* in this group, its output appended to *log*."""
        with log.open("ab") as output:
            return await asyncio.create_subprocess_exec(
                *argv,
                env=dict(env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
                process_group=self.id,
            )

    async def close(self, program: asyncio.subprocess.Process | None, *, grace_s: float) -> None:
        """Stop *program* (SIGTERM, SIGKILL after *grace_s*), then SIGKILL the rest of the group.

        The watcher goes with the rest, and the tether is closed. Safe to call
        again after a failure.

        Raises:
            RuntimeError: *program* or the watcher was still running
                :data:`KILL_WAIT_S` after SIGKILL.
        """
        saw_exit = False
        if program is not None and program.returncode is None:
            await _stop(program, grace_s=grace_s)
            saw_exit = True
        # While the watcher lives the group's ID is ours; without it, only right
        # after its program was seen to exit (a group with members keeps its ID).
        # EPERM is what macOS answers for a group of zombies only.
        if self.watcher.returncode is None or saw_exit:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.id, signal.SIGKILL)
        await _exited(self.watcher)
        if self._tether is not None:
            os.close(self._tether)
            self._tether = None


async def _stop(process: asyncio.subprocess.Process, *, grace_s: float) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await _exited(process)


async def _exited(process: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=KILL_WAIT_S)
    except TimeoutError:
        raise RuntimeError(
            f"process {process.pid} was still running {KILL_WAIT_S:g} s after SIGKILL"
        ) from None


async def finish(task: asyncio.Future[T]) -> T:
    """Await *task* to its end even if the caller is cancelled meanwhile.

    For cleanup that must not be abandoned halfway. A cancellation of the
    caller that arrives meanwhile is raised once *task* is done; otherwise
    *task*'s own result or exception is.
    """
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.done():
                break  # the task itself was cancelled: its result says so
            cancelled = cancelled or exc
    if cancelled is not None:
        raise cancelled
    return task.result()


def log_tail(log: Path, *, lines: int = 20) -> str:
    """The last *lines* of *log*, for an error message; empty when it cannot be read."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
