"""The session's process signal guard refuses made-up PIDs and lets owned processes through."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess

import pytest

from spectragent.core.capabilities.browser.backends import local_chrome
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from tests._helpers.signal_guard import (
    EXITED_PROCESS_KEPT_S,
    Entry,
    ProcessTable,
    SignalGuard,
    UnownedProcessSignal,
)

#: The made-up PID whose real SIGKILLs started this guard.
_MADE_UP_PID = 4321
_OWN_PID = 100
#: The process group of the shell that runs pytest, which pytest does not lead.
_SHELL_GROUP = 50


class _Delivered:
    """Stands in for the real ``os.kill`` / ``os.killpg``: records what would have been sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, target: int, signum: int) -> None:
        self.calls.append((target, signum))

    @property
    def targets(self) -> list[int]:
        return [target for target, _ in self.calls]


class _Machine:
    """A process table a test edits as processes come and go, and its clock."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.table: ProcessTable = {_OWN_PID: Entry(1, _SHELL_GROUP, 0.0)}

    def spawn(self, pid: int, *, parent: int, group: int | None = None) -> None:
        self.table[pid] = Entry(parent, pid if group is None else group, self.now)

    def orphan(self, pid: int) -> None:
        self.table[pid] = self.table[pid]._replace(parent=1)

    def group_of(self, pid: int) -> int:
        if pid not in self.table:
            raise ProcessLookupError(pid)
        return self.table[pid].group

    def guard(self, *started: int) -> tuple[SignalGuard, _Delivered]:
        delivered = _Delivered()
        guard = SignalGuard(
            own_pid=_OWN_PID,
            kill=delivered,
            killpg=delivered,
            group_of=self.group_of,
            processes=lambda: dict(self.table),
            clock=lambda: self.now,
        )
        for pid in started:
            guard.record_started(pid)
        return guard, delivered


@pytest.fixture
def machine() -> _Machine:
    return _Machine()


# ── decisions, on a made-up process table ───────────────────────────


@pytest.mark.parametrize("unrelated_process_has_it", [True, False])
@pytest.mark.parametrize("call", ["kill", "killpg", "kill_group"])
def test_a_made_up_pid_is_refused_before_anything_is_sent(
    machine: _Machine, call: str, unrelated_process_has_it: bool
) -> None:
    if unrelated_process_has_it:
        machine.spawn(_MADE_UP_PID, parent=1)
    guard, delivered = machine.guard()
    with pytest.raises(UnownedProcessSignal, match=f"{_MADE_UP_PID}.*SIGKILL.*not"):
        if call == "kill":
            guard.guarded_kill(_MADE_UP_PID, signal.SIGKILL)
        elif call == "killpg":
            guard.guarded_killpg(_MADE_UP_PID, signal.SIGKILL)
        else:
            guard.guarded_kill(-_MADE_UP_PID, signal.SIGKILL)
    assert delivered.calls == []
    assert len(guard.take_violations()) == 1


def test_every_process_and_the_shells_group_are_refused(machine: _Machine) -> None:
    """``kill(-1)`` reaches every process of the user; pytest's group belongs to its shell."""
    machine.spawn(_SHELL_GROUP, parent=1)
    guard, delivered = machine.guard()
    with pytest.raises(UnownedProcessSignal):
        guard.guarded_kill(-1, signal.SIGTERM)
    with pytest.raises(UnownedProcessSignal):
        guard.guarded_killpg(_SHELL_GROUP, signal.SIGTERM)
    with pytest.raises(UnownedProcessSignal):
        guard.guarded_kill(_SHELL_GROUP, signal.SIGTERM)
    assert delivered.calls == []


def test_the_test_process_and_what_it_started_are_signalled(machine: _Machine) -> None:
    machine.spawn(200, parent=_OWN_PID)
    guard, delivered = machine.guard(200)
    guard.guarded_kill(_OWN_PID, signal.SIGUSR1)
    guard.guarded_kill(200, signal.SIGTERM)
    guard.guarded_killpg(200, signal.SIGKILL)
    guard.guarded_kill(-200, signal.SIGKILL)
    assert delivered.calls == [
        (_OWN_PID, signal.SIGUSR1),
        (200, signal.SIGTERM),
        (200, signal.SIGKILL),
        (-200, signal.SIGKILL),
    ]


def test_descendants_and_members_of_a_started_group_are_signalled(machine: _Machine) -> None:
    """A Chrome launched by a launcher subprocess, its helpers, and a helper started later."""
    machine.spawn(200, parent=_OWN_PID, group=_SHELL_GROUP)  # the launcher
    machine.spawn(300, parent=200)  # its Chrome, leading its own group
    machine.spawn(301, parent=300, group=300)  # a helper
    guard, delivered = machine.guard(200)
    guard.guarded_kill(301, signal.SIGKILL)
    guard.guarded_killpg(300, signal.SIGKILL)
    machine.spawn(302, parent=1, group=300)  # reparented before any signal saw it
    guard.guarded_kill(302, signal.SIGKILL)
    assert delivered.targets == [301, 300, 302]


def test_descendants_orphaned_by_a_signal_stay_signallable(machine: _Machine) -> None:
    machine.spawn(200, parent=_OWN_PID, group=_SHELL_GROUP)
    machine.spawn(300, parent=200)
    machine.spawn(301, parent=300, group=300)
    guard, delivered = machine.guard(200)
    guard.guarded_kill(200, signal.SIGKILL)  # the launcher dies, its Chrome is reparented
    del machine.table[200]
    machine.orphan(300)
    machine.now += 1
    guard.guarded_kill(300, signal.SIGKILL)
    guard.guarded_kill(301, signal.SIGKILL)
    assert delivered.targets == [200, 300, 301]


def test_a_group_outlives_its_started_leader(machine: _Machine) -> None:
    """Chrome's helpers are killed by group right after their browser was seen to exit."""
    machine.spawn(300, parent=_OWN_PID)
    guard, delivered = machine.guard(300)
    machine.spawn(301, parent=300, group=300)
    guard.guarded_kill(300, signal.SIGTERM)
    del machine.table[300]
    machine.orphan(301)
    machine.now += 6
    guard.guarded_killpg(300, signal.SIGKILL)
    del machine.table[301]
    guard.guarded_killpg(300, signal.SIGKILL)  # nothing left: the real call reports it
    guard.guarded_kill(300, signal.SIGKILL)
    assert delivered.targets == [300, 300, 300, 300]


def test_a_browser_that_exited_before_any_signal_still_owns_its_group(machine: _Machine) -> None:
    machine.spawn(300, parent=_OWN_PID)
    guard, delivered = machine.guard(300)
    machine.spawn(301, parent=1, group=300)
    del machine.table[300]
    guard.guarded_killpg(300, signal.SIGKILL)
    assert delivered.targets == [300]


def test_an_exited_process_is_forgotten_after_a_while(machine: _Machine) -> None:
    machine.spawn(300, parent=_OWN_PID)
    guard, delivered = machine.guard(300)
    del machine.table[300]
    machine.now += EXITED_PROCESS_KEPT_S + 1
    with pytest.raises(UnownedProcessSignal):
        guard.guarded_killpg(300, signal.SIGKILL)
    assert delivered.calls == []
    guard.take_violations()


def test_a_number_handed_to_another_process_is_not_allowed(machine: _Machine) -> None:
    """A PID is remembered with when it was alive; a process started later is someone else."""
    machine.spawn(300, parent=_OWN_PID)
    guard, delivered = machine.guard(300)
    machine.now += 1
    machine.spawn(300, parent=1)  # the same number, now an unrelated group leader
    with pytest.raises(UnownedProcessSignal):
        guard.guarded_kill(300, signal.SIGKILL)
    with pytest.raises(UnownedProcessSignal):
        guard.guarded_killpg(300, signal.SIGKILL)
    assert delivered.calls == []
    assert len(guard.take_violations()) == 2


def test_signal_zero_only_asks_and_passes_for_any_pid(machine: _Machine) -> None:
    guard, delivered = machine.guard()
    guard.guarded_kill(_MADE_UP_PID, 0)
    guard.guarded_killpg(_MADE_UP_PID, 0)
    assert delivered.calls == [(_MADE_UP_PID, 0), (_MADE_UP_PID, 0)]


# ── the installed guard ─────────────────────────────────────────────


@pytest.fixture
def delivered(process_signal_guard: SignalGuard, monkeypatch: pytest.MonkeyPatch) -> _Delivered:
    """The session guard, with whatever it lets through recorded instead of sent."""
    recorder = _Delivered()
    monkeypatch.setattr(process_signal_guard, "kill", recorder)
    monkeypatch.setattr(process_signal_guard, "killpg", recorder)
    return recorder


def test_the_session_guard_stands_in_front_of_the_os(
    process_signal_guard: SignalGuard, delivered: _Delivered
) -> None:
    assert process_signal_guard.own_pid == os.getpid()
    with pytest.raises(UnownedProcessSignal, match=r"os\.killpg\(4321, SIGKILL\)"):
        os.killpg(_MADE_UP_PID, signal.SIGKILL)
    with pytest.raises(UnownedProcessSignal, match=r"os\.kill\(4321, SIGKILL\)"):
        os.kill(_MADE_UP_PID, signal.SIGKILL)
    assert delivered.calls == []
    assert len(process_signal_guard.take_violations()) == 2


async def test_a_fake_chrome_handle_cannot_kill_a_real_process_group(
    process_signal_guard: SignalGuard, delivered: _Delivered
) -> None:
    """The incident: a fake browser's made-up PID reaching ``close()``'s real group kill."""

    class FakeBrowser:
        pid = _MADE_UP_PID
        returncode: int | None = None

        def send_signal(self, _signum: int) -> None:
            self.returncode = 0

        async def wait(self) -> int:
            return 0

    owned = local_chrome._OwnedChrome(FakeBrowser(), None, tether=-1)  # type: ignore[arg-type]
    with pytest.raises(UnownedProcessSignal, match="process group 4321"):
        await LocalChromeProvider()._stop_chrome(owned)
    assert delivered.calls == []
    assert len(process_signal_guard.take_violations()) == 1


def test_a_swallowed_refusal_still_fails_the_test(
    process_signal_guard: SignalGuard, delivered: _Delivered
) -> None:
    from tests.conftest import _fail_if_refused

    with contextlib.suppress(Exception):  # what careless code under test would do
        os.killpg(_MADE_UP_PID, signal.SIGKILL)
    with pytest.raises(pytest.fail.Exception, match="Refused signal in this test"):
        _fail_if_refused(process_signal_guard, "in this test")
    assert process_signal_guard.take_violations() == []
    assert delivered.calls == []


# ── real processes this test starts ─────────────────────────────────


def test_a_started_process_and_its_group_can_be_killed() -> None:
    alone = subprocess.Popen(["sleep", "60"])
    leader = subprocess.Popen(["sleep", "60"], process_group=0)
    try:
        os.kill(alone.pid, signal.SIGKILL)
        os.killpg(leader.pid, signal.SIGKILL)
        assert alone.wait(timeout=10) == -signal.SIGKILL
        assert leader.wait(timeout=10) == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):  # just reaped: reported, not refused
            os.killpg(leader.pid, signal.SIGKILL)
    finally:
        for process in (alone, leader):
            if process.poll() is None:
                process.kill()
                process.wait()


async def test_an_asyncio_subprocess_can_be_killed() -> None:
    process = await asyncio.create_subprocess_exec("sleep", "60")
    process.kill()
    assert await asyncio.wait_for(process.wait(), 10) == -signal.SIGKILL


def test_grandchildren_can_be_killed_also_once_orphaned(
    process_signal_guard: SignalGuard,
) -> None:
    """Two grandchildren: one reached by its parent link, one after its parent was killed."""
    script = "sleep 60 & echo $!; sleep 60 & echo $!; wait"
    shell = subprocess.Popen(["sh", "-c", script], stdout=subprocess.PIPE, text=True)
    grandchildren: list[int] = []
    killed = 0
    try:
        assert shell.stdout is not None
        grandchildren = [int(shell.stdout.readline()) for _ in range(2)]
        assert not set(grandchildren) & process_signal_guard.known.keys()
        os.kill(grandchildren[0], signal.SIGKILL)  # through the parent link
        killed += 1
        os.kill(shell.pid, signal.SIGKILL)
        shell.wait(timeout=10)
        assert grandchildren[1] in process_signal_guard.known
        os.kill(grandchildren[1], signal.SIGKILL)  # orphaned: recorded when its parent was killed
        killed += 1
    finally:
        if shell.poll() is None:
            shell.kill()
            shell.wait()
        for pid in grandchildren[killed:]:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
