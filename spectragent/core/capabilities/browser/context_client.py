"""A run's control connection, restricted to its BrowserContext."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from spectragent.core.capabilities.browser.cdp_client import CDPClient, PendingReply
from spectragent.core.capabilities.browser.context_access import BrowserContextAccess

logger = logging.getLogger(__name__)


class _ContextReply:
    def __init__(self, reply: PendingReply, method: str, access: BrowserContextAccess) -> None:
        self._reply, self._method, self._access = reply, method, access

    async def wait(self, timeout: float | None = None) -> Any:
        return self._access.response(self._method, await self._reply.wait(timeout))

    def abandon(self) -> None:
        self._reply.abandon()


class BrowserContextClient:
    """Own a control transport; expose only this run's sessions and targets."""

    def __init__(self, client: CDPClient, context_id: str) -> None:
        self._client = client
        self._access = BrowserContextAccess(context_id, client)
        self._callbacks: dict[str, list[Callable[..., Any]]] = {}
        self._dispatchers: dict[str, Callable[..., Any]] = {}
        for method in ("Target.attachedToTarget", "Target.detachedFromTarget"):
            self._subscribe(method)

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    async def start(self) -> None:
        await self._client.start()

    async def stop(self) -> None:
        for method, callback in self._dispatchers.items():
            self._client.off_event(method, callback)
        self._dispatchers.clear()
        self._callbacks.clear()
        await self._client.stop()

    def add_close_callback(self, callback: Callable[[BaseException | None], None]) -> None:
        self._client.add_close_callback(callback)

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        checked = await self._access.command(method, params, session_id)
        if method == "Target.detachFromTarget" and self._access.is_retired_session(
            str(checked.get("sessionId", ""))
        ):
            return {}
        result = await self._client.send(method, checked, session_id, timeout)
        return self._access.response(method, result)

    async def post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> _ContextReply:
        checked = await self._access.command(method, params, session_id)
        return _ContextReply(
            await self._client.post(method, checked, session_id), method, self._access
        )

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        self._subscribe(method)
        self._callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        callbacks = self._callbacks.get(method, [])
        if callback in callbacks:
            callbacks.remove(callback)

    def _subscribe(self, method: str) -> None:
        if method in self._dispatchers:
            return

        async def dispatch(params: dict[str, Any], session_id: str | None = None) -> None:
            if not self._access.accept_event(method, params, session_id):
                return
            for callback in tuple(self._callbacks.get(method, ())):
                try:
                    result = callback(params, session_id)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("Browser context callback failed for %s", method)

        self._dispatchers[method] = dispatch
        self._client.on_event(method, dispatch)
