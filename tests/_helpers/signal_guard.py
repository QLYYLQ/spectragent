"""The test session signals only processes it started.

On 2026-10-23 unit-test fakes with made-up PIDs (4321, 4322) reached new code
that calls ``os.killpg``, and real SIGKILLs went to whatever process groups of
the machine had those numbers. :class:`SignalGuard` stands in front of
``os.kill`` and ``os.killpg`` for the whole session and refuses a signal aimed
at anything this test process did not start, before it is sent.

A process counts as started by this one when it is:

- this process itself;
- created through ``subprocess.Popen``, which is also what
  ``asyncio.create_subprocess_exec`` uses, so every Chrome a test launches;
- a live descendant of this process or of one started here, found by walking
  parent links in the process table (a Chrome launched by a launcher
  subprocess, Chrome's helpers);
- a descendant recorded when an earlier signal was let through, even once
  orphaned: killing a parent is what cuts its children's link back to here.

A process group counts when its leader counts, when one of its members counts,
or when its leader was started here and exited shortly before (a group outlives
its leader: Chrome's helpers are killed by group right after their browser
exits). A target that is none of these is refused even if no such process
exists, so a made-up PID is caught whatever the machine happens to be running.

Every decision reads the process table afresh. A process is remembered with a
moment it was known to be alive and is recognised by having started no later
than that, so a number the system has since handed to another process does not
stay allowed. ``Popen.send_signal``, ``Popen.kill`` and ``Popen.terminate`` reach
the guard through ``os.kill``. ``signal.pthread_kill`` needs no guard: it can
only reach a thread of this process. Signal 0 delivers nothing (it only asks
whether the PID exists), so it passes for any target.

A refused signal raises :class:`UnownedProcessSignal` at the call site and is
recorded; ``tests/conftest.py`` fails the test it happened in even when the code
under test swallowed the exception.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import pytest

#: How long a started process that has exited still covers its PID and its
#: process group. Chrome's helpers are killed by group right after the browser
#: is seen to exit, within the same ``close()`` (its ``shutdown_timeout`` is 5 s),
#: and a test's cleanup may signal a child that just exited. A minute covers
#: both, while the system hands a PID out again only after cycling through the
#: whole PID range (they are assigned in sequence).
EXITED_PROCESS_KEPT_S = 60.0


class Entry(NamedTuple):
    parent: int
    group: int
    #: When it started, to the second (``ps`` prints no finer).
    started_at: float


#: pid -> entry for every process on the machine.
ProcessTable = dict[int, Entry]


class UnownedProcessSignal(AssertionError):
    """A signal aimed at a process this test session did not start was refused."""


def read_process_table() -> ProcessTable:
    """The machine's process table, through the POSIX ``ps`` every supported platform ships."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,pgid=,lstart="],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "LC_ALL": "C"},  # an English, fixed ``lstart`` format
    ).stdout
    table: ProcessTable = {}
    for line in listing.splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) == 4:
            started = time.mktime(time.strptime(fields[3].strip(), "%a %b %d %H:%M:%S %Y"))
            table[int(fields[0])] = Entry(int(fields[1]), int(fields[2]), started)
    return table


@dataclass
class Known:
    """A process started here: a moment it was alive, and whether it led a process group then."""

    alive_at: float
    leads_group: bool


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return str(signum)


@dataclass
class SignalGuard:
    """Refuses ``os.kill`` / ``os.killpg`` aimed at a process this session did not start."""

    own_pid: int = field(default_factory=os.getpid)
    #: The real functions, called for a signal that is let through.
    kill: Callable[[int, int], None] = os.kill
    killpg: Callable[[int, int], None] = os.killpg
    #: The process group of a live PID (``ProcessLookupError`` once it exited).
    group_of: Callable[[int], int] = os.getpgid
    processes: Callable[[], ProcessTable] = read_process_table
    clock: Callable[[], float] = time.time
    #: Live processes started here or seen among the descendants.
    known: dict[int, Known] = field(default_factory=dict)
    #: Processes started here that have since exited, as they were last known.
    exited: dict[int, Known] = field(default_factory=dict)
    #: Refusals not yet reported by a test.
    violations: list[str] = field(default_factory=list)
    _reading: threading.local = field(default_factory=threading.local, init=False, repr=False)

    # ── what the patched functions call ──────────────────────────────

    def guarded_kill(self, pid: int, signum: int) -> None:
        if signum != 0:
            call = f"os.kill({pid}, {_signal_name(signum)})"
            if pid == -1:
                self._refuse(call, "it targets every process of this user")
            elif pid <= 0:
                self._check(call, group=-pid if pid else os.getpgrp())
            else:
                self._check(call, pid=pid)
        self.kill(pid, signum)

    def guarded_killpg(self, group: int, signum: int) -> None:
        if signum != 0:
            self._check(
                f"os.killpg({group}, {_signal_name(signum)})",
                group=group if group else os.getpgrp(),
            )
        self.killpg(group, signum)

    def record_started(self, pid: int | None) -> None:
        """Remember a process this one has just created."""
        if not pid:
            return
        try:
            leads_group = self.group_of(pid) == pid
        except ProcessLookupError:
            leads_group = False  # already exited; the table still lists it until reaped
        self.known[pid] = Known(self.clock(), leads_group)

    def take_violations(self) -> list[str]:
        taken, self.violations = self.violations, []
        return taken

    # ── decisions ────────────────────────────────────────────────────

    def _check(self, call: str, *, pid: int | None = None, group: int | None = None) -> None:
        if getattr(self._reading, "active", False):
            return  # the guard's own ``ps``, stopped while the table was being read
        table = self._table()
        if pid is not None:
            allowed = self._owns_process(pid, table) or (
                pid in table and self._owns_group(table[pid].group, table)
            )
            reason = f"process {pid} was not started by this test session"
        else:
            assert group is not None
            allowed = self._owns_group(group, table)
            reason = f"process group {group} was not created by this test session"
        if not allowed:
            self._refuse(call, reason)
        self._remember_descendants(table)

    def _owns_process(self, pid: int, table: ProcessTable) -> bool:
        if pid == self.own_pid or pid in self.known:
            return True
        if pid not in table:
            # Exited a moment ago: the real call reports that it is gone.
            return pid in self.exited
        return self._descends(pid, table)

    def _owns_group(self, group: int, table: ProcessTable) -> bool:
        if group in table:  # its leader is alive, so it decides
            return self._owns_process(group, table)
        if group in self.exited:
            return self.exited[group].leads_group
        return any(
            entry.group == group and self._owns_process(member, table)
            for member, entry in table.items()
        )

    def _descends(self, pid: int, table: ProcessTable) -> bool:
        """Whether parent links from ``pid`` reach this process or a live one started here."""
        seen: set[int] = set()
        while pid in table and pid not in seen:
            seen.add(pid)
            pid = table[pid].parent
            if pid == self.own_pid or pid in self.known:
                return True
        return False

    def _remember_descendants(self, table: ProcessTable) -> None:
        """Record every current descendant before a signal may orphan it."""
        children: dict[int, list[int]] = {}
        for pid, entry in table.items():
            children.setdefault(entry.parent, []).append(pid)
        now = self.clock()
        pending = [self.own_pid, *self.known]
        while pending:
            for child in children.get(pending.pop(), ()):
                if child not in self.known:
                    self.known[child] = Known(now, table[child].group == child)
                    pending.append(child)

    def _table(self) -> ProcessTable:
        """Read the process table and bring what is remembered up to date with it."""
        self._reading.active = True
        try:
            table = self.processes()
        finally:
            self._reading.active = False
        now = self.clock()
        for pid, known in list(self.known.items()):
            entry = table.get(pid)
            if entry is None or entry.started_at > known.alive_at:
                # Exited; a later start means the number belongs to another process.
                del self.known[pid]
                self.exited[pid] = known
            else:
                self.known[pid] = Known(now, entry.group == pid)
        for pid, known in list(self.exited.items()):
            if pid in table or now - known.alive_at > EXITED_PROCESS_KEPT_S:
                del self.exited[pid]
        return table

    def _refuse(self, call: str, reason: str) -> None:
        message = (
            f"Refused {call}: {reason}. A test must never signal a process it did "
            "not start; a fake process handle's made-up PID has probably reached real "
            "signalling code. Replace the function that sends the signal in the test "
            "instead (tests/conftest.py, process signal guard)."
        )
        self.violations.append(message)
        raise UnownedProcessSignal(message)

    # ── installation ─────────────────────────────────────────────────

    @contextlib.contextmanager
    def installed(self) -> Iterator[SignalGuard]:
        """Patch ``os.kill``, ``os.killpg`` and ``subprocess.Popen`` until the block ends."""
        popen_init = subprocess.Popen.__init__
        guard = self

        def recording_init(popen: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
            try:
                popen_init(popen, *args, **kwargs)
            finally:
                guard.record_started(getattr(popen, "pid", None))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "kill", self.guarded_kill)
            patch.setattr(os, "killpg", self.guarded_killpg)
            patch.setattr(subprocess.Popen, "__init__", recording_init)
            yield self
