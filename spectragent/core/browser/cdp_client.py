"""CDPClient -- WebSocket CDP client with built-in fan-out event dispatch.

Replaces cdp-use CDPClient + CDPClientAdapter.  Sends JSON-RPC 2.0 messages
over a single browser-level WebSocket, routing tab commands via ``session_id``.

Satisfies ``agent_cdp.CDPCommandProtocol`` directly -- no adapter layer needed.

Usage::

    client = CDPClient("ws://localhost:9222/devtools/browser/...")
    await client.start()
    result = await client.send("Page.navigate", {"url": "https://example.com"}, session_id=sid)
    client.on_event("Page.loadEventFired", my_callback)
    await client.stop()
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Callable
from typing import Any

import websockets

logger = logging.getLogger(__name__)


class CDPClient:
    """WebSocket CDP client with JSON-RPC 2.0 and fan-out event dispatch.

    Implements ``agent_cdp.CDPCommandProtocol`` (structural typing):
    ``is_connected``, ``send``, ``on_event``, ``off_event``.
    """

    def __init__(
        self,
        url: str,
        *,
        additional_headers: dict[str, str] | None = None,
        max_frame_size: int = 100 * 1024 * 1024,
    ) -> None:
        self._url = url
        self._additional_headers = additional_headers
        self._max_frame_size = max_frame_size

        self._ws: websockets.ClientConnection | None = None
        self._msg_id: int = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._callbacks: dict[str, list[Callable[..., Any]]] = {}
        self._recv_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to the CDP WebSocket endpoint and start the receive loop."""
        if self._ws is not None:
            raise RuntimeError("CDPClient already started")

        self._ws = await websockets.connect(
            self._url,
            max_size=self._max_frame_size,
            additional_headers=self._additional_headers,
        )
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name="cdp-recv-loop",
        )
        logger.debug("CDPClient connected to %s", self._url)

    async def stop(self) -> None:
        """Cancel the receive loop, fail pending futures, and close the WebSocket."""
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None

        self._fail_pending(ConnectionError("CDPClient stopped"))

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.debug("CDPClient stopped")

    async def __aenter__(self) -> CDPClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # CDPCommandProtocol
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Send a CDP command and return the result.

        Raises ``RuntimeError`` on CDP protocol errors or if not connected.
        """
        if self._ws is None:
            raise RuntimeError("CDPClient not connected")

        self._msg_id += 1
        msg_id = self._msg_id

        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        if session_id is not None:
            msg["sessionId"] = session_id

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future

        try:
            await self._ws.send(json.dumps(msg))
        except Exception as exc:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"Failed to send CDP message: {exc}") from exc

        return await future

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        """Register an event callback.  Multiple callbacks per method supported."""
        self._callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        """Unregister an event callback."""
        cbs = self._callbacks.get(method)
        if cbs is None:
            return
        try:
            cbs.remove(callback)
        except ValueError:
            pass
        if not cbs:
            del self._callbacks[method]

    # ------------------------------------------------------------------
    # Internal: receive loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """Background task: read WebSocket messages and route them."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("CDPClient: non-JSON message: %s", raw[:200])
                    continue

                if "id" in msg:
                    self._handle_response(msg)
                elif "method" in msg:
                    await self._dispatch_event(
                        msg["method"],
                        msg.get("params", {}),
                        msg.get("sessionId"),
                    )
                else:
                    logger.warning("CDPClient: unexpected message: %s", msg)

        except websockets.ConnectionClosed:
            logger.debug("CDPClient: WebSocket connection closed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("CDPClient: recv loop error")
        finally:
            self._fail_pending(ConnectionError("WebSocket connection lost"))
            self._ws = None

    def _handle_response(self, msg: dict[str, Any]) -> None:
        """Match a response to its pending future."""
        msg_id = msg["id"]
        future = self._pending.pop(msg_id, None)
        if future is None:
            logger.warning("CDPClient: response for unknown id %s", msg_id)
            return
        if future.done():
            return

        if "error" in msg:
            err = msg["error"]
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            future.set_exception(RuntimeError(f"CDP error: {err_msg}"))
        else:
            future.set_result(msg.get("result", {}))

    async def _dispatch_event(
        self,
        method: str,
        params: dict[str, Any],
        session_id: str | None,
    ) -> None:
        """Fan-out an event to all registered callbacks."""
        cbs = self._callbacks.get(method)
        if not cbs:
            return
        for cb in list(cbs):
            try:
                result = cb(params, session_id)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("CDPClient: event callback error for %s", method)

    def _fail_pending(self, exc: Exception) -> None:
        """Set exception on all pending request futures and clear them."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
