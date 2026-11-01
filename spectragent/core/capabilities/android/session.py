"""The device session: one device's serial work queue and its capture fan-out.

``docs/architecture_design.md`` §11.2 ("设备会话层", decided 2026-10-31). Written
here rather than taken from agent-cdp, which this package never imports
(§11.2 gives the reasons; ``test_agent_cdp_boundary.py`` enforces it).

**The queue.** Captures and actions take turns on one device, one job at a
time, in the order they were submitted. A job's wait for its turn and its run
are limited separately: time spent waiting is never taken from the run. A job
whose wait ends (its limit, or its caller cancelled) is withdrawn and never
runs. A job runs in its caller's task, so a cancelled job unwinds its own
cleanup before the next job starts and before its caller sees the
cancellation. The four lessons agent-cdp 0.5 taught are this module's tests
(``tests/test_core/test_capabilities/test_android_session.py``):

- a job never waits for itself: a job that submits another job to its own
  device is refused at once instead of deadlocking;
- the wait limit and the run limit are separate;
- a job that is cancelled does not stop the queue;
- :meth:`DeviceSession.flush` confirms that every job submitted before it has
  finished.

**The fan-out.** One capture asks its sources (the screenshot, the foreground
app, ...) at once, each within its own timeout, and hands over every answer or
none (:func:`fan_out`).

Event streams (accessibility events, logcat) join this layer with the
device-side service of a later stage.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

#: The device sessions whose turn the current task (and every task it started) holds.
_HOLDING: ContextVar[frozenset[DeviceSession]] = ContextVar(
    "android_device_turns", default=frozenset()
)


class DeviceBusyError(TimeoutError):
    """A job waited its whole wait limit for its turn; it was withdrawn and never ran."""


class DeviceJobTimeout(TimeoutError):
    """A job ran past its run limit; it was cancelled and had exited when this was raised."""


class ReentrantJobError(RuntimeError):
    """A job submitted another job to its own device, which would wait for it forever."""


class DeviceSession:
    """The serial work queue of one device."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        # asyncio.Lock wakes its waiters first come, first served: the queue's order.
        self._turn = asyncio.Lock()

    async def run(
        self, work: Callable[[], Awaitable[T]], *, what: str, wait_s: float, run_s: float
    ) -> T:
        """Run *work* when the device's earlier jobs are done, and return what it returns.

        Args:
            work: The job, started only once it has the device.
            what: What the job is, for error messages.
            wait_s: How long it may wait for its turn.
            run_s: How long it may run once started.

        Raises:
            DeviceBusyError: It did not get its turn within *wait_s*.
            DeviceJobTimeout: It ran longer than *run_s*.
            ReentrantJobError: A job of this device submitted it.
        """
        if self in _HOLDING.get():
            raise ReentrantJobError(
                f"{what}: a job of device {self.serial} submitted it, and it would wait "
                f"for that job to finish forever"
            )
        try:
            async with asyncio.timeout(wait_s):
                await self._turn.acquire()
        except TimeoutError:
            raise DeviceBusyError(
                f"{what}: device {self.serial} stayed busy for {wait_s:g} s; the job was "
                f"withdrawn without running"
            ) from None
        holding = _HOLDING.set(_HOLDING.get() | {self})
        limit = asyncio.timeout(run_s)
        try:
            async with limit:
                return await work()
        except TimeoutError:
            if limit.expired():
                raise DeviceJobTimeout(
                    f"{what}: ran longer than {run_s:g} s on device {self.serial} and was cancelled"
                ) from None
            raise
        finally:
            _HOLDING.reset(holding)
            self._turn.release()

    async def flush(self, *, wait_s: float) -> None:
        """Return once every job submitted before this call has finished or been withdrawn.

        Raises:
            DeviceBusyError: They had not within *wait_s*.
        """
        await self.run(_nothing, what="flush", wait_s=wait_s, run_s=wait_s)


async def _nothing() -> None:
    return None


@dataclass(frozen=True)
class Source(Generic[T]):
    """One source of a capture: how to ask it, and how long to wait for its answer."""

    read: Callable[[], Awaitable[T]]
    timeout_s: float


class CaptureError(Exception):
    """A source of a capture failed, so nothing of the capture is handed over."""


async def fan_out(sources: Mapping[str, Source[T]]) -> dict[str, T]:
    """Ask every source at once, each within its own timeout; return all answers or raise.

    Every source is awaited to its end, so none is left running when this
    returns or raises.

    Raises:
        CaptureError: One or more sources failed or ran out of time; each is
            named with its reason, and the first failure is the cause.
    """
    names = list(sources)
    results = await asyncio.gather(*(_ask(sources[name]) for name in names), return_exceptions=True)
    answers: dict[str, T] = {}
    failures: list[str] = []
    first: BaseException | None = None
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            failures.append(f"{name}: {type(result).__name__}: {result}")
            first = first or result
        else:
            answers[name] = result
    if failures:
        raise CaptureError("; ".join(failures)) from first
    return answers


async def _ask(source: Source[T]) -> T:
    limit = asyncio.timeout(source.timeout_s)
    try:
        async with limit:
            return await source.read()
    except TimeoutError:
        if limit.expired():
            raise TimeoutError(f"no answer within {source.timeout_s:g} s") from None
        raise
