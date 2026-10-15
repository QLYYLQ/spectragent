"""Execution-owned lifecycle: outcome fidelity, bounded cleanup and resource use."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.lifecycle import close_models, model_session


class _Model(ChatModelBase):
    __abstract__ = True
    model = "local-test"
    name = "local-test"


class _Resource:
    def __init__(self, error: Exception | None = None) -> None:
        self.closes = 0
        self.error = error

    async def close(self) -> None:
        self.closes += 1
        if self.error:
            raise self.error


def _owned(model: _Model, resource: _Resource, name: str = "http") -> _Resource:
    return model.client_resources.get_or_create(
        name, lambda: resource, close=lambda item: item.close()
    )


async def test_unused_cleanup_is_lazy_and_repeated_cleanup_is_idempotent() -> None:
    model = _Model()
    assert await model.aclose() == ()
    assert not hasattr(model, "_client_resources")
    resource = _owned(model, _Resource())
    assert await model.aclose() == ()
    assert await model.aclose() == ()
    assert resource.closes == 1


async def test_one_execution_reuses_then_next_execution_starts_fresh() -> None:
    model = _Model()
    async with model_session(model):
        first = _owned(model, _Resource())
        assert _owned(model, _Resource()) is first
        assert first.closes == 0
    async with model_session(model):
        second = _owned(model, _Resource())
        assert second is not first
    assert first.closes == second.closes == 1


async def test_borrowed_resource_is_never_closed() -> None:
    model = _Model()
    resource = _Resource()
    model.client_resources.own("borrowed", resource, close=None)
    await model.aclose()
    await model.aclose()
    assert resource.closes == 0


async def test_cleanup_failure_is_logged_without_changing_success(caplog: Any) -> None:
    model = _Model()
    result = {"reply": "ok", "usage": 42}
    async with model_session(model):
        _owned(model, _Resource(RuntimeError("close broke")))
        received = result
    assert received is result
    assert "close broke" in caplog.text


async def test_cleanup_failure_preserves_original_exception() -> None:
    model = _Model()
    cause = ValueError("request failed")
    with pytest.raises(ValueError) as error:
        async with model_session(model):
            _owned(model, _Resource(RuntimeError("close broke")))
            raise cause
    assert error.value is cause


async def test_cleanup_failure_preserves_external_cancellation() -> None:
    started = asyncio.Event()
    model = _Model()

    async def work() -> None:
        async with model_session(model):
            _owned(model, _Resource(RuntimeError("close broke")))
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_independent_sync_async_resources_all_attempted() -> None:
    model = _Model()
    closed: list[str] = []

    def sync_close(_value: object) -> None:
        closed.append("sync")
        raise RuntimeError("sync failed")

    async def async_close(_value: object) -> None:
        closed.append("async")
        raise ValueError("async failed")

    model.client_resources.own("sync", object(), close=sync_close)
    model.client_resources.own("async", object(), close=async_close)
    diagnostics = await model.aclose()
    assert set(closed) == {"sync", "async"}
    assert {row.resource for row in diagnostics} == {"sync", "async"}
    assert all(row.status == "failed" for row in diagnostics)


async def test_slow_cleanup_does_not_block_independent_resource_or_next_execution() -> None:
    model = _Model()
    stalled = asyncio.Event()
    finished = _Resource()

    async def wait(_value: object) -> None:
        await stalled.wait()

    model.client_resources.own("slow", object(), close=wait)
    _owned(model, finished)
    diagnostics = await asyncio.wait_for(model.aclose(timeout=0.03), timeout=0.5)
    assert [(row.resource, row.status) for row in diagnostics] == [("slow", "timeout")]
    assert finished.closes == 1
    replacement = _owned(model, _Resource())
    assert replacement is not finished
    await model.aclose()


async def test_sync_close_timeout_keeps_event_loop_responsive() -> None:
    model = _Model()
    release = threading.Event()
    entered = threading.Event()

    def slow(_value: object) -> None:
        entered.set()
        release.wait(timeout=2)

    model.client_resources.own("slow", object(), close=slow)
    try:
        diagnostics = await asyncio.wait_for(model.aclose(timeout=0.03), timeout=0.5)
        assert entered.is_set()
        assert diagnostics[0].status == "timeout"
    finally:
        release.set()


async def test_cleanup_waits_for_active_request() -> None:
    model = _Model()
    resource = _owned(model, _Resource())
    entered, release = asyncio.Event(), asyncio.Event()
    resources = model.client_resources

    async def call() -> None:
        async with resources.use():
            entered.set()
            await release.wait()
            assert resource.closes == 0

    request = asyncio.create_task(call())
    await entered.wait()
    closing = asyncio.create_task(model.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await request
    assert await closing == ()
    assert resource.closes == 1


async def test_active_timeout_reports_in_use_and_closes_only_after_exit() -> None:
    model = _Model()
    resource = _owned(model, _Resource())
    resources = model.client_resources
    async with resources.use():
        diagnostics = await model.aclose(timeout=0.01)
        assert diagnostics[0].status == "in_use"
        assert resource.closes == 0
    for _ in range(50):
        if resource.closes:
            break
        await asyncio.sleep(0.001)
    assert resource.closes == 1


async def test_cancelled_sync_request_retains_resource_until_thread_finishes() -> None:
    model = _Model()
    resource = _owned(model, _Resource())
    resources = model.client_resources
    release, entered = threading.Event(), threading.Event()

    def call() -> None:
        entered.set()
        release.wait(timeout=2)
        assert resource.closes == 0

    request = asyncio.create_task(resources.run_sync(call))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    try:
        diagnostics = await model.aclose(timeout=0.01)
        assert diagnostics[0].status == "in_use"
        assert resource.closes == 0
    finally:
        release.set()
    for _ in range(50):
        if resource.closes:
            break
        await asyncio.sleep(0.001)
    assert resource.closes == 1


async def test_initialization_failure_preserves_previously_registered_resources() -> None:
    model = _Model()
    transport = _Resource()

    def build() -> object:
        model.client_resources.own("transport", transport, close=lambda value: value.close())
        raise ValueError("wrapper initialization failed")

    with pytest.raises(ValueError, match="wrapper initialization"):
        async with model_session(model):
            model.client_resources.get_or_create("wrapper", build, close=None)
    assert transport.closes == 1


async def test_close_models_does_not_let_one_broken_extension_block_others() -> None:
    class Broken:
        name = "broken-extension"

        async def aclose(self, *, timeout: float = 5.0) -> tuple[Any, ...]:
            raise RuntimeError("broken close")

    model = _Model()
    resource = _owned(model, _Resource())
    diagnostics = await close_models([Broken(), model])
    assert resource.closes == 1
    assert diagnostics[0].model == "broken-extension"


def test_same_model_across_sequential_event_loops_uses_new_owned_resources() -> None:
    model = _Model()
    seen: list[_Resource] = []

    async def execute() -> None:
        async with model_session(model):
            seen.append(_owned(model, _Resource()))

    asyncio.run(execute())
    asyncio.run(execute())
    assert seen[0] is not seen[1]
    assert [resource.closes for resource in seen] == [1, 1]
