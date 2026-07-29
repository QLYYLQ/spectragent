"""``blocking=False`` handlers must be owned, and drained at run end.

Two independent failures of ``asyncio.create_task(...)`` with the handle
thrown away, both reproduced against the old code:

* the loop keeps only a *weak* reference, so a handler that has not been
  scheduled yet can be garbage-collected mid-flight ("Task was destroyed
  but it is pending!");
* nothing ever waits for it, so ``asyncio.run()`` returning abandons the
  task and the handler simply never executes.

Latent today only because both shipped hooks are ``blocking=True``. The
designed ``live_monitor`` (docs/architecture_design.md §4.6) is not.
"""

from __future__ import annotations

import asyncio
import gc

from spectragent.core.aop.advice import AdviceType
from spectragent.core.aop.proxy import _build_wrapped_method
from spectragent.core.aop.tasks import BackgroundTasks


class _NonBlocking:
    blocking = False
    hook_id = "live_monitor"


class _Blocking:
    blocking = True
    hook_id = "trace_writer"


async def _target(x: int = 0) -> int:
    return x


class TestBackgroundTasksOwnership:
    async def test_handler_survives_a_gc_pass(self) -> None:
        fired: list[str] = []

        async def handler(result, *, args, kwargs, **ctx):
            await asyncio.sleep(0)
            fired.append("nb")

        tasks = BackgroundTasks()
        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_NonBlocking(), AdviceType.AFTER, handler)],
            method_name="_target",
            background=tasks,
        )

        await wrapped(1)
        # The bug: with only asyncio's weak reference, this collects the
        # task before it is ever scheduled.
        gc.collect()
        assert [t for t in tasks.pending if not t.done()]

        await tasks.drain()
        assert fired == ["nb"]

    async def test_drain_is_what_makes_the_handler_run(self) -> None:
        fired: list[str] = []

        async def handler(result, *, args, kwargs, **ctx):
            await asyncio.sleep(0.01)
            fired.append("nb")

        tasks = BackgroundTasks()
        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_NonBlocking(), AdviceType.AFTER, handler)],
            method_name="_target",
            background=tasks,
        )

        await wrapped(1)
        assert fired == []  # the call did not wait — that is what blocking=False buys
        await tasks.drain()
        assert fired == ["nb"]

    async def test_blocking_handler_never_reaches_the_background(self) -> None:
        fired: list[str] = []

        async def handler(result, *, args, kwargs, **ctx):
            fired.append("blocking")

        tasks = BackgroundTasks()
        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_Blocking(), AdviceType.AFTER, handler)],
            method_name="_target",
            background=tasks,
        )

        await wrapped(1)
        assert fired == ["blocking"]  # already done when the call returned
        assert len(tasks) == 0

    async def test_before_advice_is_owned_too(self) -> None:
        fired: list[str] = []

        async def handler(*, args, kwargs, **ctx):
            fired.append("before")

        tasks = BackgroundTasks()
        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_NonBlocking(), AdviceType.BEFORE, handler)],
            method_name="_target",
            background=tasks,
        )

        await wrapped(1)
        await tasks.drain()
        assert fired == ["before"]

    async def test_handlers_inside_an_around_chain_are_owned(self) -> None:
        fired: list[str] = []

        async def handler(result, *, args, kwargs, **ctx):
            fired.append("after")

        async def around(*, proceed, args, kwargs, **ctx):
            return await proceed()

        tasks = BackgroundTasks()
        wrapped = _build_wrapped_method(
            _target,
            interceptors=[
                (_Blocking(), AdviceType.AROUND, around),
                (_NonBlocking(), AdviceType.AFTER, handler),
            ],
            method_name="_target",
            background=tasks,
        )

        await wrapped(1)
        await tasks.drain()
        assert fired == ["after"]

    async def test_a_proxy_with_no_owner_still_holds_its_tasks(self) -> None:
        """No caller-supplied owner is degraded, not broken: no GC loss."""
        fired: list[str] = []

        async def handler(result, *, args, kwargs, **ctx):
            fired.append("nb")

        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_NonBlocking(), AdviceType.AFTER, handler)],
            method_name="_target",
        )

        await wrapped(1)
        gc.collect()
        await asyncio.sleep(0)
        assert fired == ["nb"]


class TestDrainPolicy:
    async def test_a_failing_handler_is_logged_not_raised(self, caplog) -> None:
        async def handler(result, *, args, kwargs, **ctx):
            raise RuntimeError("monitor socket died")

        tasks = BackgroundTasks()
        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_NonBlocking(), AdviceType.AFTER, handler)],
            method_name="_target",
            background=tasks,
        )

        with caplog.at_level("ERROR"):
            assert await wrapped(1) == 1
            await tasks.drain()
        assert any("live_monitor" in rec.message for rec in caplog.records)

    async def test_a_stuck_handler_is_cancelled_and_named(self, caplog) -> None:
        """A dead WebSocket must not hold up every task's teardown."""
        started = asyncio.Event()

        async def handler(result, *, args, kwargs, **ctx):
            started.set()
            await asyncio.sleep(60)

        tasks = BackgroundTasks()
        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_NonBlocking(), AdviceType.AFTER, handler)],
            method_name="_target",
            role="env",
            background=tasks,
        )

        await wrapped(1)
        await started.wait()
        with caplog.at_level("WARNING"):
            await tasks.drain(timeout=0.01)

        assert len(tasks) == 0
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("live_monitor" in m and "cancelling" in m for m in messages)

    async def test_drain_is_idempotent(self) -> None:
        fired: list[str] = []

        async def handler(result, *, args, kwargs, **ctx):
            fired.append("nb")

        tasks = BackgroundTasks()
        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_NonBlocking(), AdviceType.AFTER, handler)],
            method_name="_target",
            background=tasks,
        )

        await wrapped(1)
        await tasks.drain()
        await tasks.drain()
        assert fired == ["nb"]
