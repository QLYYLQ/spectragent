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

Transport options (``docs/architecture_design.md`` §4.14, measured against
Chrome 153):

- ``max_frame_size`` is the largest incoming message accepted. A message over
  it closes the whole connection (websockets answers 1009) and fails every
  pending command. ``None`` is unlimited — the recorder connection's setting,
  because ``Fetch.requestPaused`` carries a large upload twice.
- ``compression`` is passed through. websockets offers permessage-deflate by
  default and Chrome accepts it; large frames are then 13–48 times slower.
- ``proxy`` is passed through. websockets (>=15) defaults to ``True``, which
  reads ``http_proxy`` from the environment, so a loopback CDP endpoint is
  tunnelled through whatever local proxy the machine exports. ``None`` is a
  direct connection.

Liveness is pushed, not polled: :meth:`CDPClient.add_close_callback`
registers a callback the receive loop calls once when the connection ends —
with the transport error, or with ``None`` when :meth:`CDPClient.stop` closed
it on purpose.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import json
import logging
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlparse

import websockets

logger = logging.getLogger(__name__)

#: The control connection's default incoming-message limit, af's own setting.
#: Chrome's limits differ by direction: a message it *reads* is capped at
#: 100 MiB (its DevTools HTTP server read buffer), a reply it *sends* at
#: 256 MiB (its send buffer, over which the reply is dropped and the socket
#: stays open). A reply between the two closes this connection (1009) although
#: Chrome sent it. No body travels here — the recorder connection carries them,
#: with no frame limit — and the largest ``DOMSnapshot`` measured is 23 MB
#: (``docs/architecture_design.md`` §4.14, "消息上限").
DEFAULT_MAX_FRAME_SIZE = 100 * 1024 * 1024

#: What a close callback receives: the transport error that ended the
#: connection, or ``None`` when :meth:`CDPClient.stop` ended it.
CloseCallback = Callable[[BaseException | None], None]


def transport_options(url: str) -> dict[str, Any]:
    """Transport options for the *control* connection to ``url``.

    A loopback endpoint (``localhost``, ``127.0.0.0/8``, ``::1``) — the
    browser this machine launched — is reached directly and uncompressed:

    - ``proxy=None``: websockets' default honours ``http_proxy``, which
      tunnels loopback CDP through whatever local proxy the machine exports
      (measured with a Clash proxy in the path).
    - ``compression=None``: measured on Chrome 153 against one target through
      two clients, ``DOMSnapshot.captureSnapshot`` of a 23 MB page took 1066 ms
      uncompressed vs 1237 ms with permessage-deflate (median of 9; p90 1083
      vs 1318), a 300-element page 15.2 vs 18.0 ms, and the replies were
      byte-identical (same sha256) — compression is lossless and buys nothing
      on loopback.

    A remote endpoint keeps websockets' defaults (the environment's proxy,
    which a cloud provider may need; compression, which saves bandwidth on a
    real network and was not measured there): ``{}``.
    """
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return {"proxy": None, "compression": None}
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    return {"proxy": None, "compression": None} if loopback else {}


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
        max_frame_size: int | None = DEFAULT_MAX_FRAME_SIZE,
        compression: str | None = "deflate",
        proxy: str | Literal[True] | None = True,
    ) -> None:
        self._url = url
        self._additional_headers = additional_headers
        self._max_frame_size = max_frame_size
        self._compression = compression
        self._proxy: str | Literal[True] | None = proxy

        self._ws: websockets.ClientConnection | None = None
        self._msg_id: int = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        #: Ids whose caller gave up (timeout or cancellation). Their late
        #: replies are dropped silently instead of being logged as unknown.
        self._abandoned: set[int] = set()
        self._callbacks: dict[str, list[Callable[..., Any]]] = {}
        self._close_callbacks: list[CloseCallback] = []
        self._recv_task: asyncio.Task[None] | None = None
        #: Set by :meth:`stop` so the receive loop reports a deliberate close
        #: as ``None`` rather than as a lost connection.
        self._stopping = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to the CDP WebSocket endpoint and start the receive loop."""
        if self._ws is not None:
            raise RuntimeError("CDPClient already started")

        self._stopping = False
        self._ws = await websockets.connect(
            self._url,
            max_size=self._max_frame_size,
            additional_headers=self._additional_headers,
            compression=self._compression,
            proxy=self._proxy,
        )
        self._recv_task = asyncio.create_task(
            self._recv_loop(self._ws),
            name="cdp-recv-loop",
        )
        logger.debug("CDPClient connected to %s", self._url)

    async def stop(self) -> None:
        """Cancel the receive loop, fail pending futures, and close the WebSocket.

        A deliberate close: close callbacks receive ``None``. Closing the
        socket is what tells Chrome this client is gone, so it is done even
        when the receive loop already let go of it.
        """
        self._stopping = True
        ws = self._ws
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
            self._recv_task = None

        self.fail_pending(ConnectionError("CDPClient stopped"))

        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        self._ws = None
        # A receive loop cancelled before it ever ran has no ``finally`` to
        # report from. Reporting twice is impossible: a fired callback list
        # is emptied, so after a loss this is a no-op.
        self._fire_close_callbacks(None)

        logger.debug("CDPClient stopped")

    async def __aenter__(self) -> CDPClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    def add_close_callback(self, callback: CloseCallback) -> None:
        """Call ``callback`` once when this connection ends.

        It receives the transport error that ended the connection, or
        ``None`` when :meth:`stop` closed it. Called synchronously from the
        receive loop's ``finally``, after every pending command has been
        failed; an exception it raises is logged, never propagated.
        """
        self._close_callbacks.append(callback)

    def fail_pending(self, exc: BaseException) -> None:
        """Fail every command still waiting for a reply with ``exc``.

        Public because a connection can become unusable for a reason the
        transport cannot see: when the recorder connection is lost, the
        capability fails the *control* connection's waiters with an error
        naming the recorder, so no observation or action completes
        unrecorded (§4.14).
        """
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        self._abandoned.clear()

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
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send a CDP command and return the result.

        ``timeout`` bounds the wait for the reply. On expiry the request is
        forgotten (a late reply is dropped) and ``TimeoutError`` names the
        method. ``None`` waits as long as the connection lives.

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

        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._abandon(msg_id)
            raise TimeoutError(
                f"CDP {method} got no reply within {timeout:g}s"
                + (f" (session {session_id})" if session_id else "")
            ) from None
        except asyncio.CancelledError:
            self._abandon(msg_id)
            raise

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        """Register an event callback.  Multiple callbacks per method supported."""
        self._callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        """Unregister an event callback."""
        cbs = self._callbacks.get(method)
        if cbs is None:
            return
        with contextlib.suppress(ValueError):
            cbs.remove(callback)
        if not cbs:
            del self._callbacks[method]

    # ------------------------------------------------------------------
    # Internal: receive loop
    # ------------------------------------------------------------------

    async def _recv_loop(self, ws: websockets.ClientConnection) -> None:
        """Background task: read WebSocket messages and route them."""
        reason: BaseException | None = None
        try:
            async for raw in ws:
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
            # The iterator ends quietly on a normal close frame from the peer.
            reason = ConnectionError(
                f"CDP WebSocket closed by the peer (code {ws.close_code}, "
                f"reason {ws.close_reason!r})"
            )
        except websockets.ConnectionClosed as exc:
            logger.debug("CDPClient: WebSocket connection closed: %s", exc)
            reason = exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("CDPClient: recv loop error")
            reason = exc
            # Nobody reads this socket any more: close it, so the browser sees
            # the client go rather than a client that stopped answering. A
            # silent client holding waitForDebuggerOnStart wedges every new
            # target in the browser; a closed one is detached (measured, §4.14).
            with contextlib.suppress(Exception):
                await ws.close()
        finally:
            if self._stopping:
                reason = None
            elif reason is None:
                # Cancelled by something other than stop(): still a loss.
                reason = ConnectionError("CDP WebSocket receive loop was cancelled")
            self.fail_pending(
                ConnectionError("CDPClient stopped")
                if reason is None
                else ConnectionError(f"WebSocket connection lost: {reason}")
            )
            self._ws = None
            self._fire_close_callbacks(reason)

    def _fire_close_callbacks(self, reason: BaseException | None) -> None:
        callbacks, self._close_callbacks = self._close_callbacks, []
        for callback in callbacks:
            try:
                callback(reason)
            except Exception:
                logger.exception("CDPClient: close callback raised")

    def _handle_response(self, msg: dict[str, Any]) -> None:
        """Match a response to its pending future."""
        msg_id = msg["id"]
        future = self._pending.pop(msg_id, None)
        if future is None:
            if msg_id in self._abandoned:
                self._abandoned.discard(msg_id)
                return
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

    def _abandon(self, msg_id: int) -> None:
        """Forget a request whose caller stopped waiting for it."""
        if self._pending.pop(msg_id, None) is not None:
            self._abandoned.add(msg_id)

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
