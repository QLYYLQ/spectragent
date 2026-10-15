"""Explicit ownership of resources shared by runs in one experiment process.

Binding is synchronous assembly. A resource factory must not acquire external
resources; its owner opens lazily when a run actually uses it. The scope never
discovers browser concepts, rewires a graph, or records run statistics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, runtime_checkable

from spectragent.core.composer import ComposedRun

ResourceT = TypeVar("ResourceT")
InstanceIdentity = tuple[str, str]


@runtime_checkable
class ExperimentParticipant(Protocol):
    """An assembled component that can borrow an experiment-owned resource."""

    def bind_experiment(self, scope: ExperimentScope, *, identity: InstanceIdentity) -> None: ...


@dataclass
class _Resource:
    configuration: Hashable
    value: Any
    close: Callable[[], Awaitable[None]]
    closing: asyncio.Task[None] | None = None


class ExperimentScope:
    """Own shared resources until all runs in this explicit scope have ended.

    The same composed instance is bound once. Across fresh composed runs the
    stable layer/instance identity selects a resource, and contradictory
    configuration is refused instead of silently reusing the first launch.
    """

    def __init__(self, *, cleanup_timeout_s: float = 15.0) -> None:
        if cleanup_timeout_s <= 0:
            raise ValueError("cleanup_timeout_s must be positive")
        self.cleanup_timeout_s = cleanup_timeout_s
        self._resources: dict[Hashable, _Resource] = {}
        self._bound: dict[int, object] = {}
        self._active = False
        self._closing: asyncio.Task[None] | None = None

    async def __aenter__(self) -> ExperimentScope:
        if self._active or self._closing is not None:
            raise RuntimeError("An experiment scope cannot be entered twice")
        self._active = True
        return self

    async def __aexit__(self, _type: Any, error: BaseException | None, _tb: Any) -> None:
        try:
            await self.aclose()
        except BaseException as cleanup_error:
            if isinstance(cleanup_error, asyncio.CancelledError):
                if error is not None:
                    cleanup_error.add_note(f"Experiment body failed before cancellation: {error!r}")
                raise cleanup_error from error
            if error is None:
                raise
            error.add_note(f"Experiment resource cleanup also failed: {cleanup_error!r}")

    def bind(self, run: ComposedRun) -> ComposedRun:
        """Bind participating instances without acquiring any external resource."""
        if not self._active:
            raise RuntimeError("Bind runs inside an active ExperimentScope")
        for layer, instances in run.by_layer.items():
            for name, instance in instances.items():
                if id(instance) in self._bound:
                    continue
                if isinstance(instance, ExperimentParticipant):
                    instance.bind_experiment(self, identity=(layer, name))
                    self._bound[id(instance)] = instance
        return run

    def resource(
        self,
        key: Hashable,
        *,
        configuration: Hashable,
        create: Callable[[], ResourceT],
        close: Callable[[ResourceT], Awaitable[None]],
    ) -> ResourceT:
        """Get an identity's owner, constructed once; *create* performs no I/O."""
        if not self._active:
            raise RuntimeError("Experiment resources require an active scope")
        existing = self._resources.get(key)
        if existing is not None:
            if existing.configuration != configuration:
                raise ValueError(f"Experiment resource {key!r} has conflicting configuration")
            return existing.value
        value = create()
        self._resources[key] = _Resource(configuration, value, lambda: close(value))
        return value

    async def aclose(self) -> None:
        """Attempt every owner, with a finite budget; preserve caller cancellation."""
        self._active = False
        if self._closing is None or self._closing.done():
            self._closing = asyncio.create_task(self._close_resources())
        task = self._closing
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except BaseException:
                break
        try:
            task.result()
        except BaseException as exc:
            if cancellation is None:
                raise
            cancellation.add_note(f"Experiment cleanup also failed: {exc!r}")
        if cancellation is not None:
            raise cancellation

    async def _close_resources(self) -> None:
        errors: list[Exception] = []
        for key, resource in reversed(tuple(self._resources.items())):

            async def close_owner(owner: _Resource = resource) -> None:
                await owner.close()

            if resource.closing is None or resource.closing.done():
                resource.closing = asyncio.create_task(close_owner())
            task = resource.closing
            done, _ = await asyncio.wait({task}, timeout=self.cleanup_timeout_s)
            if not done:
                task.cancel()
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                errors.append(
                    TimeoutError(f"Experiment resource {key!r} cleanup remains unfinished")
                )
                continue
            try:
                task.result()
            except BaseException as exc:
                errors.append(RuntimeError(f"Experiment resource {key!r} cleanup failed: {exc!r}"))
            else:
                self._resources.pop(key, None)
        self._bound.clear()
        if errors:
            raise ExceptionGroup("Experiment resource cleanup failed", errors)
