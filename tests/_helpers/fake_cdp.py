"""A scripted browser behind fake CDP clients, for the recorder choreography.

``FakeBrowser.client`` has the shape of ``CDPClient(url, **options)``, so it
plugs into ``BrowserCapability._inject_cdp_factory`` (control and recorder
connections both) or straight into ``RecorderConnection``. Every client
records what it sent, in order, with the session it went to; the browser
answers the commands the capability's setup and the recorder's pipeline
use, routes events to the client that caused them, and can be told to fail
a method, hold a reply until something else happens, or drop a connection.

It models the session facts the recorder depends on (S1 / S2a):

- browser-level ``Target.setAutoAttach`` attaches every existing target to
  *that* client before it replies, not waiting;
- a target created later is attached to every client with auto-attach on,
  ``waitingForDebugger: true`` for those that asked to wait;
- ``Runtime.runIfWaitingForDebugger`` must arrive on the target's own
  session (a session-less one — PausedTarget mode A — resumes nothing);
- on a waiting service worker, ``Network.*`` / ``Runtime.*`` do not answer
  until the target is resumed.

For the archivers (M29) it also answers what a collection and a DOM capture
read: response bodies (:attr:`FakeBrowser.response_bodies`), cookies and
Web Storage, a small document for ``DOM.getDocument`` / ``DOMSnapshot`` /
the accessibility tree, the fingerprint probe (and its isolated world), and a
streamed ``Network.loadNetworkResource`` (:attr:`FakeBrowser.resources`).
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Sent:
    method: str
    params: dict[str, Any]
    session_id: str | None
    timeout: float | None = None


@dataclass
class FakeTarget:
    target_id: str
    type: str = "page"
    url: str = "about:blank"
    opener_id: str | None = None
    #: client id → session on that client (auto-attached or attached).
    sessions: dict[int, str] = field(default_factory=dict)
    #: sessions still waiting for ``Runtime.runIfWaitingForDebugger``.
    waiting: set[str] = field(default_factory=set)

    def info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"targetId": self.target_id, "type": self.type, "url": self.url}
        if self.opener_id:
            info["openerId"] = self.opener_id
        return info


class FakeClient:
    """One connection to the :class:`FakeBrowser`."""

    def __init__(self, browser: FakeBrowser, url: str, options: dict[str, Any]) -> None:
        self.browser = browser
        self.url = url
        self.options = options
        self.id = len(browser.clients)
        self.sent: list[Sent] = []
        self.callbacks: dict[str, list[Callable[..., Any]]] = {}
        self.close_callbacks: list[Callable[[BaseException | None], None]] = []
        self.started = False
        self.stopped = False
        self.connected = False
        self.auto_attach: dict[str | None, dict[str, Any]] = {}
        self._pending: set[asyncio.Future[Any]] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self.failed_with: BaseException | None = None

    # ── CDPClient surface ─────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def start(self) -> None:
        self.started = True
        self.connected = True

    async def stop(self) -> None:
        self.stopped = True
        was_connected = self.connected
        self.connected = False
        self._fail(ConnectionError("CDPClient stopped"))
        for task in list(self._handlers):
            task.cancel()
        if was_connected:
            self._fire_close(None)

    def add_close_callback(self, callback: Callable[[BaseException | None], None]) -> None:
        self.close_callbacks.append(callback)

    def fail_pending(self, exc: BaseException) -> None:
        self.failed_with = exc
        self._fail(exc)

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        self.callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        cbs = self.callbacks.get(method, [])
        if callback in cbs:
            cbs.remove(callback)

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if not self.connected:
            raise RuntimeError("CDPClient not connected")
        self.sent.append(Sent(method, dict(params or {}), session_id, timeout))
        reply: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending.add(reply)
        handler = asyncio.ensure_future(self._answer(reply, method, dict(params or {}), session_id))
        self._handlers.add(handler)
        handler.add_done_callback(self._handlers.discard)
        try:
            if timeout is None:
                return await reply
            return await asyncio.wait_for(reply, timeout)
        except TimeoutError:
            raise TimeoutError(f"CDP {method} got no reply within {timeout:g}s") from None
        finally:
            self._pending.discard(reply)

    async def _answer(
        self,
        reply: asyncio.Future[Any],
        method: str,
        params: dict[str, Any],
        session_id: str | None,
    ) -> None:
        try:
            result = await self.browser.handle(self, method, params, session_id)
        except Exception as exc:
            if not reply.done():
                reply.set_exception(exc)
        else:
            if not reply.done():
                reply.set_result(result)

    # ── test controls ─────────────────────────────────────────────────

    def fire(self, method: str, params: dict[str, Any], session_id: str | None = None) -> None:
        """Dispatch an event the way the receive loop does (callbacks in order)."""
        for callback in list(self.callbacks.get(method, [])):
            result = callback(params, session_id)
            assert not asyncio.iscoroutine(result), f"{method} callback must be synchronous"

    def lose(self, reason: BaseException) -> None:
        """The connection drops on its own."""
        self.connected = False
        self._fail(ConnectionError(f"WebSocket connection lost: {reason}"))
        self._fire_close(reason)

    def methods(self, *, session_id: Any = ..., prefix: str = "") -> list[str]:
        return [
            s.method
            for s in self.sent
            if (session_id is ... or s.session_id == session_id) and s.method.startswith(prefix)
        ]

    def _fire_close(self, reason: BaseException | None) -> None:
        callbacks, self.close_callbacks = self.close_callbacks, []
        for callback in callbacks:
            callback(reason)

    def _fail(self, exc: BaseException) -> None:
        for future in list(self._pending):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


class FakeBrowser:
    """A scripted Chrome. ``browser.client`` is the transport factory."""

    def __init__(self, *, user_agent: str = "FakeChrome/1.0") -> None:
        self.user_agent = user_agent
        self.clients: list[FakeClient] = []
        self.targets: dict[str, FakeTarget] = {"page-1": FakeTarget("page-1")}
        self._sessions = 0
        #: method → exception to raise when it is sent (optionally per session).
        self.failures: dict[tuple[str, str | None], BaseException] = {}
        #: method → event a reply waits for (optionally per session).
        self.holds: dict[tuple[str, str | None], asyncio.Event] = {}
        #: Sessions resumed via a session-less runIfWaitingForDebugger (mode A).
        self.sessionless_resumes = 0
        self.loader_id = "loader-1"
        #: The origin every frame reports (``Page.getFrameTree.securityOrigin``).
        self.origin = "http://shop.test"
        #: Fetch interception id → the body ``Fetch.getResponseBody`` returns.
        self.response_bodies: dict[str, bytes] = {}
        #: Network request id → ``Network.getRequestPostData``'s body.
        self.post_data: dict[str, bytes] = {}
        #: ``Storage.getCookies``.
        self.cookies: list[dict[str, Any]] = []
        #: origin → items, for local and session storage.
        self.local_storage: dict[str, dict[str, str]] = {}
        self.session_storage: dict[str, dict[str, str]] = {}
        #: ``Network.loadNetworkResource`` URL → the bytes its stream yields.
        self.resources: dict[str, bytes] = {}
        self._streams: dict[str, bytes] = {}
        #: ``Page.createIsolatedWorld`` calls answered (the fingerprint probe's world).
        self.isolated_worlds = 0
        #: What the page reports to the fingerprint probe.
        self.timezone = "UTC"
        self.locale = "en-US"
        self.device_pixel_ratio = 1.0
        #: The value attribute of the document's one password input.
        self.password_value = "planted-secret"
        #: What ``Page.captureScreenshot`` returns (a PNG header and filler).
        self.screenshot = b"\x89PNG\r\n\x1a\n" + bytes(range(64))

    # ── factory ───────────────────────────────────────────────────────

    def client(self, url: str, **options: Any) -> FakeClient:
        client = FakeClient(self, url, options)
        self.clients.append(client)
        return client

    @property
    def control(self) -> FakeClient:
        return self.clients[0]

    @property
    def recorder(self) -> FakeClient:
        return self.clients[1]

    # ── scripting ─────────────────────────────────────────────────────

    def fail(self, method: str, exc: BaseException, *, session_id: str | None = None) -> None:
        self.failures[(method, session_id)] = exc

    def hold(self, method: str, *, session_id: str | None = None) -> asyncio.Event:
        event = asyncio.Event()
        self.holds[(method, session_id)] = event
        return event

    def new_session(self, prefix: str) -> str:
        self._sessions += 1
        return f"{prefix}-{self._sessions}"

    def open_target(
        self,
        target_id: str,
        type_: str,
        *,
        url: str = "",
        parent: tuple[FakeClient, str] | None = None,
    ) -> dict[int, str]:
        """A new target appears; every auto-attaching client gets it, waiting.

        ``parent`` attaches it under a session (an OOPIF or worker under its
        page) instead of at browser level. Returns client id → session.
        """
        target = FakeTarget(target_id, type_, url)
        self.targets[target_id] = target
        attached: dict[int, str] = {}
        for client in self.clients:
            scope = parent[1] if parent and parent[0] is client else None
            if parent and parent[0] is not client:
                continue
            settings = client.auto_attach.get(scope)
            if not settings or not settings.get("autoAttach"):
                continue
            sid = self.new_session(f"c{client.id}")
            target.sessions[client.id] = sid
            waiting = bool(settings.get("waitForDebuggerOnStart"))
            if waiting:
                target.waiting.add(sid)
            attached[client.id] = sid
            client.fire(
                "Target.attachedToTarget",
                {"sessionId": sid, "targetInfo": target.info(), "waitingForDebugger": waiting},
                scope,
            )
        return attached

    def is_running(self, target_id: str) -> bool:
        return not self.targets[target_id].waiting

    def _target_of(self, session_id: str | None) -> FakeTarget | None:
        for target in self.targets.values():
            if session_id in target.sessions.values():
                return target
        return None

    # ── the protocol ──────────────────────────────────────────────────

    async def handle(
        self, client: FakeClient, method: str, params: dict[str, Any], session_id: str | None
    ) -> Any:
        for key in ((method, session_id), (method, None)):
            exc = self.failures.get(key)
            if exc is not None:
                raise exc
        target = self._target_of(session_id)
        if (
            target is not None
            and target.type == "service_worker"
            and session_id in target.waiting
            and method.startswith(("Network.", "Runtime."))
            and method != "Runtime.runIfWaitingForDebugger"
        ):
            while session_id in target.waiting:
                await asyncio.sleep(0.005)
        for key in ((method, session_id), (method, None)):
            held = self.holds.get(key)
            if held is not None:
                await held.wait()

        if method == "Runtime.runIfWaitingForDebugger":
            if session_id is None:
                self.sessionless_resumes += 1
                return {}
            if target is not None:
                target.waiting.discard(session_id)
            return {}
        if method == "Target.setAutoAttach":
            client.auto_attach[session_id] = params
            if session_id is None and params.get("autoAttach"):
                for existing in list(self.targets.values()):
                    if client.id in existing.sessions or existing.type != "page":
                        continue
                    sid = self.new_session(f"c{client.id}")
                    existing.sessions[client.id] = sid
                    client.fire(
                        "Target.attachedToTarget",
                        {
                            "sessionId": sid,
                            "targetInfo": existing.info(),
                            "waitingForDebugger": False,
                        },
                        None,
                    )
            return {}
        if method == "Target.detachFromTarget":
            sid = params.get("sessionId")
            for existing in self.targets.values():
                if existing.sessions.get(client.id) == sid:
                    del existing.sessions[client.id]
                    existing.waiting.discard(str(sid))
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": [t.info() for t in self.targets.values() if t.type == "page"]}
        if method == "Target.attachToTarget":
            attached = self.targets[params["targetId"]]
            sid = self.new_session(f"c{client.id}")
            attached.sessions[client.id] = sid
            return {"sessionId": sid}
        if method == "Target.createTarget":
            target_id = f"page-{len(self.targets) + 1}"
            self.open_target(target_id, "page", url=params.get("url", ""))
            return {"targetId": target_id}
        if method == "Browser.getVersion":
            return {"product": "FakeChrome/1.0", "userAgent": self.user_agent}
        if method == "Page.getFrameTree":
            url = target.url if target is not None else ""
            return {
                "frameTree": {
                    "frame": {
                        "id": f"frame-{session_id}",
                        "loaderId": self.loader_id,
                        "url": url,
                        "securityOrigin": self.origin,
                    }
                }
            }
        if method == "Fetch.getResponseBody":
            body = self.response_bodies.get(str(params.get("requestId")), b"")
            return {"body": base64.b64encode(body).decode("ascii"), "base64Encoded": True}
        if method == "Network.getRequestPostData":
            data = self.post_data.get(str(params.get("requestId")))
            if data is None:
                raise RuntimeError("CDP error: No post data available for the request")
            return {"postData": base64.b64encode(data).decode("ascii"), "base64Encoded": True}
        if method == "Network.loadNetworkResource":
            url = str(params.get("url"))
            if url not in self.resources:
                return {"resource": {"success": False, "netErrorName": "net::ERR_FAILED"}}
            handle = f"stream-{len(self._streams) + 1}"
            self._streams[handle] = self.resources[url]
            return {"resource": {"success": True, "httpStatusCode": 200, "stream": handle}}
        if method == "IO.read":
            handle = str(params.get("handle"))
            data = self._streams.get(handle, b"")
            size = int(params.get("size") or 65536)
            chunk, self._streams[handle] = data[:size], data[size:]
            return {
                "data": base64.b64encode(chunk).decode("ascii"),
                "base64Encoded": True,
                "eof": not self._streams[handle],
            }
        if method == "IO.close":
            self._streams.pop(str(params.get("handle")), None)
            return {}
        if method == "Storage.getCookies":
            return {"cookies": [dict(cookie) for cookie in self.cookies]}
        if method == "DOMStorage.getDOMStorageItems":
            storage_id = params.get("storageId") or {}
            store = self.local_storage if storage_id.get("isLocalStorage") else self.session_storage
            items = store.get(str(storage_id.get("securityOrigin")), {})
            return {"entries": [[key, value] for key, value in items.items()]}
        if method == "Page.createIsolatedWorld":
            self.isolated_worlds += 1
            return {"executionContextId": 1000 + self.isolated_worlds}
        if method == "Runtime.evaluate":
            expression = str(params.get("expression") or "")
            if "devicePixelRatio ||" in expression:
                return {"result": {"type": "number", "value": self.device_pixel_ratio}}
            if "navigator.userAgent" in expression:
                probe = {
                    "userAgent": self.user_agent,
                    "userAgentData": None,
                    "timezone": self.timezone,
                    "locale": self.locale,
                    "languages": [self.locale],
                    "devicePixelRatio": self.device_pixel_ratio,
                }
                return {"result": {"type": "string", "value": json.dumps(probe)}}
            return {}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(self.screenshot).decode("ascii")}
        if method == "DOM.getDocument":
            return {"root": self.document()}
        if method == "DOMSnapshot.captureSnapshot":
            return {"documents": [], "strings": []}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": []}
        if method == "Page.getNavigationHistory":
            url = target.url if target is not None else ""
            return {"currentIndex": 0, "entries": [{"url": url, "title": "fake"}]}
        return {}

    def document(self) -> dict[str, Any]:
        """A small ``DOM.getDocument`` root: one text input and one password input."""
        return {
            "nodeId": 1,
            "backendNodeId": 1,
            "nodeType": 9,
            "nodeName": "#document",
            "children": [
                {
                    "nodeId": 2,
                    "backendNodeId": 2,
                    "nodeType": 1,
                    "nodeName": "HTML",
                    "attributes": [],
                    "children": [
                        {
                            "nodeId": 3,
                            "backendNodeId": 3,
                            "nodeType": 1,
                            "nodeName": "INPUT",
                            "attributes": ["name", "q", "value", "shoes"],
                        },
                        {
                            "nodeId": 4,
                            "backendNodeId": 4,
                            "nodeType": 1,
                            "nodeName": "INPUT",
                            "attributes": ["type", "password", "value", self.password_value],
                            "shadowRoots": [
                                {
                                    "nodeId": 5,
                                    "backendNodeId": 5,
                                    "nodeType": 11,
                                    "nodeName": "#document-fragment",
                                    "shadowRootType": "user-agent",
                                    "children": [
                                        {
                                            "nodeId": 6,
                                            "backendNodeId": 6,
                                            "nodeType": 3,
                                            "nodeName": "#text",
                                            "nodeValue": self.password_value,
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
