"""Provider extension API for execution-owned resources and request leases.

Providers use ClientResources through ChatModelBase.client_resources. Runtime and
other consumers end a model execution through the BaseChatModel lifecycle Protocol.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from spectragent.core.llm.lifecycle import ClientCleanupDiagnostic, report_cleanup

T = TypeVar("T")


@dataclass
class _Resource:
    value: Any
    close: Callable[[Any], Awaitable[None] | None] | None


async def _call_sync(call: Callable[[], T]) -> T:
    """A bounded caller need not wait for the interpreter's default executor."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def settle(value: Any, error: BaseException | None) -> None:
        if future.done():
            if inspect.iscoroutine(value):
                value.close()
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)

    def worker() -> None:
        value, error = None, None
        try:
            value = call()
        except BaseException as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(settle, value, error)
        except RuntimeError:
            if inspect.iscoroutine(value):
                value.close()

    threading.Thread(target=worker, name="model-client-worker", daemon=True).start()
    return await future


async def _invoke_close(resource: _Resource) -> None:
    """Sync and async public SDK close methods share the same result semantics."""
    callback = resource.close
    if callback is None:
        return
    if inspect.iscoroutinefunction(callback):
        await callback(resource.value)
        return
    result = await _call_sync(lambda: callback(resource.value))
    if inspect.isawaitable(result):
        await result


def _consume(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


class ClientResources:
    """One model execution's cache and explicitly registered close operations."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._resources: dict[str, _Resource] = {}
        self._construction = threading.RLock()
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closing = False
        self._close_task: asyncio.Task[tuple[ClientCleanupDiagnostic, ...]] | None = None
        self._pending: set[asyncio.Task[Any]] = set()

    def own(
        self,
        key: str,
        resource: T,
        *,
        close: Callable[[T], Awaitable[None] | None] | None,
    ) -> T:
        with self._construction:
            if key in self._resources:
                raise ValueError(f"Client resource already registered: {key}")
            if self._closing and not self._active:
                raise RuntimeError("Model execution is closing")
            self._resources[key] = _Resource(resource, close)
            return resource

    def get_or_create(
        self,
        key: str,
        factory: Callable[[], T],
        *,
        close: Callable[[T], Awaitable[None] | None] | None,
    ) -> T:
        with self._construction:
            if self._closing and not self._active:
                raise RuntimeError("Model execution is closing")
            resource = self._resources.get(key)
            if resource is None:
                return self.own(key, factory(), close=close)
            return cast(T, resource.value)

    @asynccontextmanager
    async def use(self) -> AsyncIterator[None]:
        if self._closing:
            raise RuntimeError("Model execution is closing")
        self._active += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._active -= 1
            if not self._active:
                self._idle.set()

    async def run_sync(self, call: Callable[[], T]) -> T:
        """Retain the resource lease until a synchronous SDK call actually exits.

        Cancelling an asyncio waiter does not stop a running SDK thread. The
        retained task releases its lease only when that thread reports back.
        """

        async def perform() -> T:
            async with self.use():
                return await _call_sync(call)

        task = asyncio.create_task(perform())
        self._retain(task)
        return await asyncio.shield(task)

    def _retain(self, task: asyncio.Task[Any]) -> None:
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        task.add_done_callback(_consume)

    async def aclose(self, *, timeout: float) -> tuple[ClientCleanupDiagnostic, ...]:
        if timeout <= 0:
            raise ValueError("Model cleanup timeout must be positive")
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._finish(timeout))
            self._close_task.add_done_callback(_consume)
        return await asyncio.shield(self._close_task)

    async def _finish(self, timeout: float) -> tuple[ClientCleanupDiagnostic, ...]:
        started = asyncio.get_running_loop().time()
        if self._active:
            waiter = asyncio.create_task(self._idle.wait())
            done, _ = await asyncio.wait({waiter}, timeout=timeout)
            if not done:
                waiter.cancel()
                self._retain(waiter)
                diagnostics = tuple(
                    ClientCleanupDiagnostic(
                        self._model, key, "in_use", "A model call still uses this resource"
                    )
                    for key, item in self._resources.items()
                    if item.close is not None
                )
                if not diagnostics:
                    diagnostics = (
                        ClientCleanupDiagnostic(
                            self._model,
                            "model",
                            "in_use",
                            "A model request or client initialization is still running",
                        ),
                    )
                for diagnostic in diagnostics:
                    report_cleanup(diagnostic)

                async def after_calls() -> None:
                    await self._idle.wait()
                    await self._close_resources(timeout)

                self._retain(asyncio.create_task(after_calls()))
                return diagnostics
        remaining = max(0.0, timeout - (asyncio.get_running_loop().time() - started))
        return await self._close_resources(remaining)

    async def _close_resources(self, timeout: float) -> tuple[ClientCleanupDiagnostic, ...]:
        resources, self._resources = self._resources, {}
        tasks = {
            asyncio.create_task(_invoke_close(resource)): key
            for key, resource in resources.items()
            if resource.close is not None
        }
        if not tasks:
            return ()
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        diagnostics: list[ClientCleanupDiagnostic] = []
        for task, key in tasks.items():
            if task in pending:
                task.cancel()
                self._retain(task)
                diagnostic = ClientCleanupDiagnostic(
                    self._model, key, "timeout", "Resource cleanup did not finish within its budget"
                )
            else:
                try:
                    task.result()
                    continue
                except asyncio.CancelledError:
                    diagnostic = ClientCleanupDiagnostic(
                        self._model, key, "cancelled", "Resource cleanup cancelled itself"
                    )
                except Exception as exc:
                    diagnostic = ClientCleanupDiagnostic(
                        self._model, key, "failed", f"{type(exc).__name__}: {exc}"
                    )
            report_cleanup(diagnostic)
            diagnostics.append(diagnostic)
        return tuple(diagnostics)
