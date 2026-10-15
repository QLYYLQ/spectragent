"""Experiment-owned Chrome process and individually releasable run contexts."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from spectragent.core.capabilities.browser.backends.base import BaseBrowserProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.schemas.provider_session import ProviderSession

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.recorder_broker import (
        ContextRecorderClient,
        RecorderBroker,
    )


class BrowserContextLease:
    """One run owns this context, but borrows the experiment's Chrome process."""

    def __init__(self, owner: SharedBrowser, context_id: str, cdp_url: str) -> None:
        self._owner = owner
        self._context_id = context_id
        self._cdp_url = cdp_url
        self._closed = False
        self._closing: asyncio.Task[None] | None = None

    @property
    def context_id(self) -> str:
        return self._context_id

    @property
    def cdp_url(self) -> str:
        return self._cdp_url

    @property
    def recorder_client(self) -> ContextRecorderClient:
        if self._closed:
            raise RuntimeError("Browser context lease is closed")
        return self._owner.recorder_client(self.context_id)

    @property
    def metadata(self) -> Mapping[str, Any]:
        """A snapshot of the actual shared launch, for this run's fingerprint."""
        return self._owner.metadata

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._closing is None or self._closing.done():
            self._closing = asyncio.create_task(self._close_context())
        cancellation: asyncio.CancelledError | None = None
        while not self._closing.done():
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except BaseException:
                break
        try:
            self._closing.result()
        except BaseException as exc:
            if cancellation is None:
                raise
            cancellation.add_note(f"Context cleanup also failed: {exc!r}")
        if cancellation is not None:
            raise cancellation

    async def _close_context(self) -> None:
        await self._owner.release(self.context_id)
        self._closed = True


class SharedBrowser:
    """Own one provider session and early-attach broker for an experiment.

    Construction is pure. The first context acquisition starts the process;
    all later runs must request exactly the same launch plan. No process or
    profile knowledge is moved out of the provider.
    """

    def __init__(self, provider: BaseBrowserProvider) -> None:
        self._provider = provider
        self._session: ProviderSession | None = None
        self._client: CDPClient | None = None
        self._broker: RecorderBroker | None = None
        self._leases: dict[str, BrowserContextLease] = {}
        self._launch_plan: tuple[tuple[str, ...], tuple[tuple[str, str], ...]] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._closing: asyncio.Task[None] | None = None

    @property
    def metadata(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._session.metadata) if self._session is not None else {}

    async def acquire(
        self,
        *,
        launch_args: Sequence[str] = (),
        launch_env: Mapping[str, str] | None = None,
        record_targets: bool = False,
    ) -> BrowserContextLease:
        """Acquire a fresh context; cancelled acquisition still disposes its result."""
        task = asyncio.create_task(
            self._acquire(tuple(launch_args), dict(launch_env or {}), record_targets)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            while not task.done():
                try:
                    with suppress(asyncio.CancelledError):
                        await asyncio.shield(task)
                except BaseException:
                    break
            try:
                lease = task.result()
                await lease.aclose()
            except BaseException as cleanup_error:
                cancellation.add_note(f"Context acquisition cleanup also failed: {cleanup_error!r}")
            raise cancellation

    async def _acquire(
        self, launch_args: tuple[str, ...], launch_env: dict[str, str], record_targets: bool
    ) -> BrowserContextLease:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Shared browser owner is closed")
            plan = (launch_args, tuple(sorted(launch_env.items())))
            if self._launch_plan is not None and plan != self._launch_plan:
                raise ValueError(
                    "Shared Chrome runs requested different launch arguments or environment"
                )
            if self._session is None:
                self._session = await self._provider.open(
                    launch_args=launch_args, launch_env=launch_env
                )
                if self._session.cdp_url is None:
                    raise RuntimeError("Shared browser provider returned no CDP endpoint")
                self._client = CDPClient(
                    self._session.cdp_url, proxy=None, compression=None, max_frame_size=None
                )
                await self._client.start()
                self._launch_plan = plan
            if record_targets and self._broker is None:
                from spectragent.core.capabilities.browser.recorder_broker import RecorderBroker

                assert self._client is not None
                self._broker = RecorderBroker(self._client)
                await self._broker.open()
            assert self._client is not None and self._session.cdp_url is not None
            result = await self._client.send(
                "Target.createBrowserContext", {"disposeOnDetach": True}, timeout=5
            )
            context_id = result["browserContextId"]
            lease = BrowserContextLease(self, context_id, self._session.cdp_url)
            self._leases[context_id] = lease
            return lease

    def recorder_client(self, context_id: str) -> ContextRecorderClient:
        if context_id not in self._leases or self._broker is None:
            raise RuntimeError("BrowserContext is not owned by this experiment")
        return self._broker.client_for_context(context_id)

    async def release(self, context_id: str) -> None:
        if context_id not in self._leases:
            return
        if self._client is None:
            raise RuntimeError("Shared browser control connection is unavailable")
        await self._client.send(
            "Target.disposeBrowserContext", {"browserContextId": context_id}, timeout=5
        )
        self._leases.pop(context_id, None)

    async def aclose(self) -> None:
        """Release all contexts and transport, then the provider's owned process."""
        if self._closing is None or self._closing.done():
            self._closing = asyncio.create_task(self._close_resources())
        cancellation: asyncio.CancelledError | None = None
        while not self._closing.done():
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except BaseException:
                break
        try:
            self._closing.result()
        except BaseException as exc:
            if cancellation is None:
                raise
            cancellation.add_note(f"Shared browser cleanup also failed: {exc!r}")
        if cancellation is not None:
            raise cancellation

    async def _close_resources(self) -> None:
        async with self._lock:
            self._closed = True
            errors: list[Exception] = []
            for context_id in tuple(self._leases):
                try:
                    await self.release(context_id)
                except Exception as exc:
                    errors.append(exc)
            if self._broker is not None:
                try:
                    await self._broker.close()
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._broker = None
                    self._client = None
            elif self._client is not None:
                try:
                    await self._client.stop()
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._client = None
            if self._session is not None:
                try:
                    await self._provider.close(self._session)
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._session = None
                    self._leases.clear()
            if errors:
                raise ExceptionGroup("Shared browser cleanup failed", errors)
