"""The device session: one job at a time, two separate limits, and the four lessons of agent-cdp 0.5.

``docs/architecture_design.md`` §11.2 ("设备会话层") lists the four: a job never
waits for itself; the wait limit and the run limit are separate; a cancelled
job does not stop the queue; flush confirms that everything submitted before it
has been processed. Each has a test below, as do withdrawal of a queued job and
the capture fan-out.

Where a test needs time to pass (a job outliving another's limit), it sleeps
for that time: the delay is the point. Everything else waits on events, with a
generous bound so a deadlock fails instead of hanging.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest

from spectragent.core.capabilities.android.session import (
    CaptureError,
    DeviceBusyError,
    DeviceJobTimeout,
    DeviceSession,
    ReentrantJobError,
    Source,
    fan_out,
)

T = TypeVar("T")

SERIAL = "emulator-5554"
_WAIT_S = 10.0


def _queue(
    session: DeviceSession,
    work: Callable[[], Awaitable[T]],
    what: str,
    *,
    wait_s: float = _WAIT_S,
    run_s: float = _WAIT_S,
) -> asyncio.Task[T]:
    return asyncio.ensure_future(session.run(work, what=what, wait_s=wait_s, run_s=run_s))


async def _bounded(awaitable: Awaitable[T]) -> T:
    async with asyncio.timeout(_WAIT_S):
        return await awaitable


class Holder:
    """A job that keeps the device until released, telling when it has it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> str:
        self.started.set()
        await self.release.wait()
        return "held"


class TestTheQueue:
    async def test_jobs_run_one_at_a_time_in_the_order_they_were_submitted(self) -> None:
        session = DeviceSession(SERIAL)
        holder = Holder()
        log: list[str] = []

        async def job(name: str) -> str:
            log.append(f"start {name}")
            await asyncio.sleep(0)
            log.append(f"end {name}")
            return name

        first = _queue(session, holder, "holder")
        await _bounded(holder.started.wait())
        rest = [_queue(session, lambda name=name: job(name), name) for name in "abc"]
        await asyncio.sleep(0)
        assert log == [], "nothing runs while the device is held"
        holder.release.set()

        assert await _bounded(asyncio.gather(first, *rest)) == ["held", "a", "b", "c"]
        assert log == ["start a", "end a", "start b", "end b", "start c", "end c"]

    async def test_a_job_never_waits_for_itself(self) -> None:
        """Lesson one: submitting to its own device from inside a job is refused at once."""
        session = DeviceSession(SERIAL)

        async def inner() -> str:
            return "inner"

        async def submits_directly() -> str:
            return await session.run(inner, what="inner", wait_s=_WAIT_S, run_s=_WAIT_S)

        async def submits_from_a_task_it_started() -> str:
            return await asyncio.ensure_future(
                session.run(inner, what="inner", wait_s=_WAIT_S, run_s=_WAIT_S)
            )

        for outer in (submits_directly, submits_from_a_task_it_started):
            with pytest.raises(ReentrantJobError, match="inner: a job of device emulator-5554"):
                await _bounded(session.run(outer, what="outer", wait_s=1.0, run_s=1.0))
        assert await _bounded(session.run(inner, what="after", wait_s=1.0, run_s=1.0)) == "inner"

    async def test_waiting_for_a_turn_is_not_taken_from_the_run_limit(self) -> None:
        """Lesson two: the wait limit and the run limit are separate."""
        session = DeviceSession(SERIAL)

        async def holds_a_while() -> None:
            await asyncio.sleep(0.3)

        async def quick() -> str:
            return "quick"

        first = _queue(session, holds_a_while, "first")
        await asyncio.sleep(0)
        # It waits 0.3 s for its turn, longer than its own 0.2 s run limit.
        second = _queue(session, quick, "second", run_s=0.2)

        await _bounded(first)
        assert await _bounded(second) == "quick"

    async def test_a_job_that_waits_past_its_wait_limit_is_withdrawn_and_never_runs(
        self,
    ) -> None:
        session = DeviceSession(SERIAL)
        holder = Holder()
        ran: list[str] = []

        async def job(name: str) -> str:
            ran.append(name)
            return name

        first = _queue(session, holder, "holder")
        await _bounded(holder.started.wait())
        with pytest.raises(DeviceBusyError, match="late: device emulator-5554 stayed busy"):
            await _bounded(session.run(lambda: job("late"), what="late", wait_s=0.05, run_s=1))
        holder.release.set()
        await _bounded(first)

        assert await _bounded(_queue(session, lambda: job("next"), "next")) == "next"
        assert ran == ["next"]

    async def test_a_job_past_its_run_limit_is_cancelled_and_has_exited_when_it_is_reported(
        self,
    ) -> None:
        session = DeviceSession(SERIAL)
        cleaned_up = asyncio.Event()

        async def never_ends() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned_up.set()

        with pytest.raises(DeviceJobTimeout, match="ran longer than 0.05 s"):
            await _bounded(session.run(never_ends, what="stuck", wait_s=1, run_s=0.05))
        assert cleaned_up.is_set()

    async def test_a_timeout_the_job_raises_itself_is_not_mistaken_for_the_run_limit(
        self,
    ) -> None:
        session = DeviceSession(SERIAL)

        async def gives_up() -> None:
            raise TimeoutError("its own deadline")

        with pytest.raises(TimeoutError, match="its own deadline") as raised:
            await _bounded(session.run(gives_up, what="job", wait_s=1, run_s=_WAIT_S))
        assert not isinstance(raised.value, DeviceJobTimeout)


class TestCancellation:
    async def test_a_cancelled_job_unwinds_before_its_caller_returns_and_the_next_job_starts(
        self,
    ) -> None:
        """Lesson three: one job being cancelled does not stop the queue."""
        session = DeviceSession(SERIAL)
        started = asyncio.Event()
        unwound: list[str] = []

        async def cancelled_one() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                for _ in range(3):  # cleanup that takes a few turns of the loop
                    await asyncio.sleep(0)
                unwound.append("cancelled job")

        async def next_one() -> list[str]:
            return list(unwound)

        victim = _queue(session, cancelled_one, "victim")
        await _bounded(started.wait())
        follower = _queue(session, next_one, "follower")
        victim.cancel()

        with pytest.raises(asyncio.CancelledError):
            await victim
        assert unwound == ["cancelled job"], "its caller saw the cancellation only after it exited"
        assert await _bounded(follower) == ["cancelled job"], "the next job started after it"

    async def test_a_queued_job_whose_caller_is_cancelled_is_withdrawn(self) -> None:
        session = DeviceSession(SERIAL)
        holder = Holder()
        ran: list[str] = []

        async def job(name: str) -> str:
            ran.append(name)
            return name

        first = _queue(session, holder, "holder")
        await _bounded(holder.started.wait())
        withdrawn = _queue(session, lambda: job("withdrawn"), "withdrawn")
        after = _queue(session, lambda: job("after"), "after")
        await asyncio.sleep(0)
        withdrawn.cancel()
        holder.release.set()

        await _bounded(asyncio.gather(first, after))
        assert withdrawn.cancelled()
        assert ran == ["after"]

    async def test_a_job_that_fails_leaves_the_queue_running(self) -> None:
        session = DeviceSession(SERIAL)

        async def fails() -> None:
            raise ValueError("bad input")

        async def fine() -> str:
            return "fine"

        with pytest.raises(ValueError, match="bad input"):
            await _bounded(session.run(fails, what="fails", wait_s=1, run_s=1))
        assert await _bounded(session.run(fine, what="fine", wait_s=1, run_s=1)) == "fine"


class TestFlush:
    async def test_flush_returns_once_every_job_submitted_before_it_has_finished(self) -> None:
        """Lesson four: flush confirms that what came before it was processed."""
        session = DeviceSession(SERIAL)
        holder = Holder()
        log: list[str] = []

        async def queued() -> None:
            await asyncio.sleep(0)
            log.append("queued job finished")

        first = _queue(session, holder, "holder")
        await _bounded(holder.started.wait())
        second = _queue(session, queued, "queued")
        flushed = asyncio.ensure_future(session.flush(wait_s=_WAIT_S))
        await asyncio.sleep(0)
        assert not flushed.done()

        holder.release.set()
        await _bounded(flushed)
        assert log == ["queued job finished"] and first.done() and second.done()

    async def test_a_flush_that_cannot_get_through_says_so(self) -> None:
        session = DeviceSession(SERIAL)
        holder = Holder()
        first = _queue(session, holder, "holder")
        await _bounded(holder.started.wait())

        with pytest.raises(DeviceBusyError, match="flush"):
            await _bounded(session.flush(wait_s=0.05))
        holder.release.set()
        await _bounded(first)


class TestFanOut:
    async def test_every_answer_is_handed_over_under_its_name(self) -> None:
        async def answer(value: str) -> str:
            await asyncio.sleep(0)
            return value

        got = await _bounded(
            fan_out(
                {
                    "screenshot": Source(lambda: answer("png"), timeout_s=1),
                    "foreground app": Source(lambda: answer("settings"), timeout_s=1),
                }
            )
        )

        assert got == {"screenshot": "png", "foreground app": "settings"}

    async def test_one_failed_source_fails_the_capture_and_the_others_are_awaited_to_their_end(
        self,
    ) -> None:
        finished: list[str] = []

        async def fails() -> str:
            raise ConnectionError("device offline")

        async def slower() -> str:
            await asyncio.sleep(0.05)
            finished.append("slower")
            return "png"

        with pytest.raises(CaptureError, match="foreground app: ConnectionError: device offline"):
            await _bounded(
                fan_out(
                    {
                        "screenshot": Source(slower, timeout_s=_WAIT_S),
                        "foreground app": Source(fails, timeout_s=_WAIT_S),
                    }
                )
            )
        assert finished == ["slower"], "no source is left running behind a failed capture"

    async def test_each_source_has_its_own_timeout(self) -> None:
        async def never() -> str:
            await asyncio.Event().wait()
            return "never"

        async def in_time() -> str:
            await asyncio.sleep(0.1)
            return "png"

        with pytest.raises(CaptureError) as failed:
            await _bounded(
                fan_out(
                    {
                        "screenshot": Source(in_time, timeout_s=_WAIT_S),
                        "foreground app": Source(never, timeout_s=0.05),
                    }
                )
            )

        assert str(failed.value) == "foreground app: TimeoutError: no answer within 0.05 s"

    async def test_cancelling_the_capture_cancels_every_source(self) -> None:
        stopped: list[str] = []
        both_waiting = asyncio.Barrier(3)

        async def waits(name: str) -> str:
            try:
                await both_waiting.wait()
                await asyncio.Event().wait()
            finally:
                stopped.append(name)
            return name

        capture = asyncio.ensure_future(
            fan_out(
                {
                    "a": Source(lambda: waits("a"), timeout_s=_WAIT_S),
                    "b": Source(lambda: waits("b"), timeout_s=_WAIT_S),
                }
            )
        )
        await _bounded(both_waiting.wait())
        capture.cancel()

        with pytest.raises(asyncio.CancelledError):
            await capture
        assert sorted(stopped) == ["a", "b"]
