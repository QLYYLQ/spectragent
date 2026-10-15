"""Ownership and draining for non-blocking hook handlers.

``blocking=False`` means the intercepted call does not wait for the
handler (see :class:`~spectragent.core.hooks.base.RuntimeHook`). The
handler therefore has to run *somewhere* after the call returns, and
something has to own it until it finishes.

``asyncio.create_task(...)`` with the handle discarded owns nothing. The
event loop keeps only a weak reference, so a handler that has not been
scheduled yet can be garbage-collected mid-flight ("Task was destroyed
but it is pending!"), and even one that survives is abandoned the moment
the loop stops — ``asyncio.run()`` returns and the handler never ran at
all. Both were reproducible against a bare ``create_task``.

:class:`BackgroundTasks` is the missing owner: a strong reference for the
lifetime of the task, plus a drain point so "the run is over" actually
means the run's side channels are over too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT = 5.0
"""Seconds :meth:`BackgroundTasks.drain` waits before cancelling.

Deliberately short. A ``blocking=False`` handler is a fire-and-forget
side channel, and one that has not finished seconds after the
run ended has nothing left to contribute. A generous timeout would be
paid once per task by every benchmark sweep, so a single dead socket
would stall hundreds of teardowns.
"""


class BackgroundTasks:
    """Strong references to in-flight non-blocking handler tasks.

    One instance per run, owned by ``AgentRuntime`` and handed to the
    ``Weaver`` — not a module-level registry. Two runs in the same
    process drain independently.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def __len__(self) -> int:
        return len(self._tasks)

    @property
    def pending(self) -> frozenset[asyncio.Task[Any]]:
        """Tasks spawned and not yet finished."""
        return frozenset(self._tasks)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, label: str) -> asyncio.Task[Any]:
        """Schedule *coro* and hold a strong reference until it finishes.

        Args:
            coro: The handler call, already wrapped in whatever
                error-isolation the caller wants.
            label: Task name used in the drain-timeout warning — make it
                say which hook on which method, or the warning names
                nothing actionable.
        """
        task = asyncio.get_running_loop().create_task(coro, name=label)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        """Wait for in-flight handlers, then cancel whatever is left.

        A handler still pending after *timeout* is cancelled and named in
        a WARNING. It is deliberately not a run-level failure: a
        non-blocking hook that cannot fail the call it fired on must not
        be able to fail the run either — that is the whole point of
        declaring it non-blocking. Silence, though, would leave a dead
        WebSocket looking exactly like a healthy one.
        """
        pending = {task for task in self._tasks if not task.done()}
        if not pending:
            return

        try:
            _, unfinished = await asyncio.wait(pending, timeout=timeout)
        except asyncio.CancelledError:
            for task in pending:
                if not task.done():
                    task.cancel()
            raise
        if not unfinished:
            return

        logger.warning(
            "%d background hook handler(s) still running %.1fs after the run ended; cancelling: %s",
            len(unfinished),
            timeout,
            sorted(task.get_name() for task in unfinished),
        )
        for task in unfinished:
            task.cancel()
        # Give cancellation a scheduling turn, never an unbounded gather:
        # a handler may suppress CancelledError or await unbounded cleanup.
        # The owner keeps strong references until those tasks actually end.
        _, remaining = await asyncio.wait(unfinished, timeout=0)
        if remaining:
            logger.warning(
                "%d background hook handler(s) still running after cancellation was requested: %s",
                len(remaining),
                sorted(task.get_name() for task in remaining),
            )
