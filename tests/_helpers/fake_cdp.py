"""A scripted browser behind fake CDP clients, for the recorder choreography.

``FakeBrowser.client`` has the shape of ``CDPClient(url, **options)``, so it
plugs into ``BrowserCapability._inject_cdp_factory`` (control and recorder
connections both) or straight into ``RecorderConnection``. Every client
records what it sent, in order, with the session it went to; the browser
answers the commands the capability's setup and the recorder's pipeline
use, routes events to the client that caused them, and can be told to fail
a method, hold a reply until something else happens, or drop a connection.

It models the session facts the recorder depends on (measured, ``docs/architecture_design.md`` §4.14):

- browser-level ``Target.setAutoAttach`` attaches every existing target to
  *that* client before it replies, not waiting;
- a target created later is attached to every client with auto-attach on,
  ``waitingForDebugger: true`` for those that asked to wait;
- ``Runtime.runIfWaitingForDebugger`` must arrive on the target's own
  session (a browser-level command with sessionId only in params resumes nothing);
- a target without a page process (:attr:`FakeTarget.page_process`: a tab
  its opener cannot reach, a waiting service worker — the default for
  service workers) answers ``Page.*``, ``Emulation.*``, ``Network.*`` and
  ``Runtime.*`` only after it is resumed and its first document committed
  (:meth:`FakeBrowser.commit`, at once unless the target was opened with
  ``commit_held``), in order, and the resume answers last, after every one
  of them sent before it.

Every client also has :meth:`FakeClient.post` (write now, wait later), and a
write can be stalled (:meth:`FakeBrowser.stall_write`: a socket that cannot
take the frame), so a test can hold a preparation before its resume is written.

For the archivers (M29) it also answers what a collection and a DOM capture
read: response bodies (:attr:`FakeBrowser.response_bodies`), cookies and
Web Storage, a small document for ``DOM.getDocument`` / ``DOMSnapshot`` /
the accessibility tree, the fingerprint probe (and its isolated world), and
``Network.streamResourceContent`` (nothing buffered: the chunks are events a
test fires).

And the observation's settle wait (``settle.py``): its isolated world, and its
in-page probe, which reports the page quiet at once
(:attr:`FakeBrowser.settle_answer`, changeable per test). And the screenshot's
media reads (``media.py``): their isolated world and a page without audio or
video (:attr:`FakeBrowser.media_answer`); a session-level ``Target.setAutoAttach``
honours its type filter, as Chrome does (frame discovery adopts iframes only).

And the measurement of the browser's defaults right after launch
(``backends/browser_defaults.py``): its isolated world and its probe, answered
with :attr:`FakeBrowser.default_timezone` / :attr:`FakeBrowser.default_locale`.
And the read of the browser's own client hints (``client_hints.py``): its
temporary ``chrome://version`` tab (``Target.createTarget`` / ``closeTarget``),
its isolated world and its probe, answered with
:attr:`FakeBrowser.client_hints` (``None``: the page reports no
``navigator.userAgentData``).

And target discovery, as following a new tab reads it (``tabs.py``, §4.14
"页面新开的标签页"): ``Target.setDiscoverTargets`` reports every existing tab
to that client before it replies, :meth:`FakeBrowser.open_target` reports a new
one (``openerId`` from ``opener_id``) before it attaches it anywhere, and
:meth:`FakeBrowser.close_target` — or ``Target.closeTarget`` — detaches every
session of the target and reports it destroyed. ``Page.windowOpen`` is a test's
to fire, on the opener's session (:meth:`FakeClient.fire`).
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from spectragent.core.capabilities.browser.backends.browser_defaults import DEFAULTS_WORLD
from spectragent.core.capabilities.browser.client_hints import CLIENT_HINTS_WORLD
from spectragent.core.capabilities.browser.color_scheme import COLOR_SCHEME_WORLD
from spectragent.core.capabilities.browser.media import MEDIA_WORLD
from spectragent.core.capabilities.browser.platform import PLATFORM_WORLD
from spectragent.core.capabilities.browser.settle import SETTLE_WORLD

#: What a target without a page process answers only once it runs and commits.
PAGE_SIDE_DOMAINS = ("Page.", "Emulation.", "Network.", "Runtime.")
#: Chrome 155's own client hints when launched with ``--user-agent`` (measured, macOS).
FAKE_CLIENT_HINTS: dict[str, Any] = {
    "brands": [
        {"brand": "Google Chrome", "version": "155"},
        {"brand": "Chromium", "version": "155"},
        {"brand": "Not(A:Brand", "version": "24"},
    ],
    "mobile": False,
    "platform": "macOS",
    "architecture": "",
    "bitness": "",
    "formFactors": [],
    "fullVersionList": [],
    "model": "",
    "platformVersion": "",
    "uaFullVersion": "",
    "wow64": False,
}
RESUME = "Runtime.runIfWaitingForDebugger"


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
    #: ``False``: Chrome built it without a page process (module docstring).
    page_process: bool = True
    #: Its first document has not committed yet although it runs (:meth:`FakeBrowser.commit`).
    commit_held: bool = False
    #: Set once it runs and its first document committed: page-side commands answer.
    running: asyncio.Event = field(default_factory=asyncio.Event)

    def info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"targetId": self.target_id, "type": self.type, "url": self.url}
        if self.opener_id:
            info["openerId"] = self.opener_id
        return info


class FakeReply:
    """A command a :class:`FakeClient` already wrote; its reply is awaited later."""

    def __init__(
        self,
        client: FakeClient,
        future: asyncio.Future[Any],
        method: str,
        session_id: str | None,
    ) -> None:
        self._client = client
        self._future = future
        self.method = method
        self.session_id = session_id

    async def wait(self, timeout: float | None = None) -> Any:
        try:
            if timeout is None:
                return await self._future
            return await asyncio.wait_for(self._future, timeout)
        except TimeoutError:
            raise TimeoutError(f"CDP {self.method} got no reply within {timeout:g}s") from None
        finally:
            self._client._pending.discard(self._future)

    def abandon(self) -> None:
        self._client._pending.discard(self._future)
        if not self._future.done():
            self._future.cancel()
        elif not self._future.cancelled():
            self._future.exception()


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
        #: The target types ``Target.setDiscoverTargets`` asked to hear about, or ``None``.
        self.discovers: frozenset[str] | None = None
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
        timeout: float | None = None,
    ) -> Any:
        reply = await self._write(method, params, session_id, timeout)
        return await reply.wait(timeout)

    async def post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> FakeReply:
        """Write now (in :attr:`sent` when this returns), wait later — ``CDPClient.post``."""
        return await self._write(method, params, session_id, None)

    async def _write(
        self,
        method: str,
        params: dict[str, Any] | None,
        session_id: str | None,
        timeout: float | None,
    ) -> FakeReply:
        if not self.connected:
            raise RuntimeError("CDPClient not connected")
        for key in ((method, session_id), (method, None)):
            stalled = self.browser.write_stalls.get(key)
            if stalled is not None:
                await stalled.wait()
        if not self.connected:
            raise RuntimeError("CDPClient not connected")
        for key in ((method, session_id), (method, None)):
            refused = self.browser.write_failures.get(key)
            if refused is not None:
                raise RuntimeError(f"Failed to send CDP message: {refused}")
        self.sent.append(Sent(method, dict(params or {}), session_id, timeout))
        reply: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending.add(reply)
        handler = asyncio.ensure_future(self._answer(reply, method, dict(params or {}), session_id))
        self._handlers.add(handler)
        handler.add_done_callback(self._handlers.discard)
        return FakeReply(self, reply, method, session_id)

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
        #: method → event the *write* waits for (optionally per session).
        self.write_stalls: dict[tuple[str, str | None], asyncio.Event] = {}
        #: method → why its write fails (optionally per session): ``CDPClient``'s ``RuntimeError``.
        self.write_failures: dict[tuple[str, str | None], str] = {}
        #: Page-side commands of a target without a page process still unanswered, per session.
        self._queued: dict[str, int] = {}
        #: Sessions of targets that closed: a command sent on one fails, as in Chrome.
        self.closed_sessions: set[str] = set()
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
        #: ``Page.createIsolatedWorld`` calls answered (the fingerprint probe's world).
        self.isolated_worlds = 0
        #: Execution contexts of the settle wait's world, and how many probes ran in them.
        self.settle_contexts: set[int] = set()
        self.settle_probes = 0
        #: What the settle probe resolves with: a quiet page, at once.
        self.settle_answer: dict[str, Any] = {
            "quiet": True,
            "waited_ms": 0,
            "mutations": 0,
            "resources": 0,
        }
        #: Execution contexts of the screenshot's media world, and the reads made in them.
        self.media_contexts: set[int] = set()
        self.media_reads = 0
        #: What a media read of a frame returns: the main document, without audio or video.
        self.media_answer: dict[str, Any] = {
            "path": "main",
            "elements": [],
            "children": [],
            "truncated": False,
        }
        #: What the page reports to the fingerprint probe.
        self.timezone = "UTC"
        self.locale = "en-US"
        #: What a frame no override reaches reads (the browser-defaults measurement).
        self.default_timezone: str | None = "UTC"
        self.default_locale: str | None = "en-US"
        #: Execution contexts of the browser-defaults measurement's world.
        self.defaults_contexts: set[int] = set()
        #: What ``getHighEntropyValues`` resolves to in the client-hints read: Chrome 155's own
        #: under ``--user-agent`` (measured: no high-entropy values). ``None``: no userAgentData.
        self.client_hints: dict[str, Any] | None = dict(FAKE_CLIENT_HINTS)
        #: Execution contexts of the client-hints read's world.
        self.client_hints_contexts: set[int] = set()
        #: The startup theme, before a replayability plan overrides it.
        self.color_scheme: str | None = "light"
        self.color_scheme_contexts: set[int] = set()
        #: Chrome's internal OS cell, independent of its simulated UA fields.
        self.browser_os: str | None = "macOS"
        self.platform_contexts: set[int] = set()
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

    def stall_write(self, method: str, *, session_id: str | None = None) -> asyncio.Event:
        """The next writes of *method* wait for the returned event (a socket that takes nothing)."""
        event = asyncio.Event()
        self.write_stalls[(method, session_id)] = event
        return event

    def commit(self, target_id: str) -> None:
        """The target's first document commits: its held page-side replies follow (if it runs)."""
        target = self.targets[target_id]
        target.commit_held = False
        if not target.waiting:
            target.running.set()

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
        page_process: bool | None = None,
        commit_held: bool = False,
        opener_id: str | None = None,
    ) -> dict[int, str]:
        """A new target appears; every auto-attaching client gets it, waiting.

        ``parent`` attaches it under a session (an OOPIF or worker under its
        page) instead of at browser level. ``page_process=False`` builds it
        without a page process (the default for a service worker), and
        ``commit_held`` keeps its first document from committing until
        :meth:`commit`. ``opener_id`` is the tab that opened it. Every
        discovering client hears of it first. Returns client id → session.
        """
        target = FakeTarget(
            target_id,
            type_,
            url,
            opener_id=opener_id,
            page_process=(type_ != "service_worker") if page_process is None else page_process,
            commit_held=commit_held,
        )
        if target.page_process:
            target.running.set()
        self.targets[target_id] = target
        for client in self.clients:
            if client.discovers is not None and type_ in client.discovers:
                client.fire("Target.targetCreated", {"targetInfo": target.info()}, None)
        attached: dict[int, str] = {}
        for client in self.clients:
            scope = parent[1] if parent and parent[0] is client else None
            if parent and parent[0] is not client:
                continue
            settings = client.auto_attach.get(scope)
            if not settings or not settings.get("autoAttach"):
                continue
            allowed = {entry.get("type") for entry in settings.get("filter") or []}
            if allowed and type_ not in allowed:
                continue  # e.g. a frame-discovery auto-attach (iframes only) never adopts a tab
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

    def close_target(self, target_id: str) -> None:
        """The target goes (``window.close()``, ``Target.closeTarget``): sessions detach, discovery hears."""
        target = self.targets.pop(target_id)
        self.closed_sessions.update(target.sessions.values())
        for client in self.clients:
            sid = target.sessions.get(client.id)
            if sid is not None:
                client.fire(
                    "Target.detachedFromTarget", {"sessionId": sid, "targetId": target_id}, None
                )
            if client.discovers is not None and target.type in client.discovers:
                client.fire("Target.targetDestroyed", {"targetId": target_id}, None)

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
        if session_id is not None and session_id in self.closed_sessions:
            raise RuntimeError("CDP error: Session with given id not found.")
        target = self._target_of(session_id)
        if (
            target is not None
            and session_id is not None
            and not target.page_process
            and method.startswith(PAGE_SIDE_DOMAINS)
            and method != RESUME
        ):
            # Answered once the target runs and its first document committed.
            self._queued[session_id] = self._queued.get(session_id, 0) + 1
            try:
                await target.running.wait()
            finally:
                self._queued[session_id] -= 1
        for key in ((method, session_id), (method, None)):
            held = self.holds.get(key)
            if held is not None:
                await held.wait()

        if method == RESUME:
            if session_id is None:
                self.sessionless_resumes += 1
                return {}
            if target is not None:
                target.waiting.discard(session_id)
                if not target.waiting and not target.commit_held:
                    target.running.set()
                # A target without a page process answers its resume last,
                # after every page-side command sent before it.
                while self._queued.get(session_id, 0):
                    await asyncio.sleep(0.002)
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
        if method == "Target.setDiscoverTargets":
            if not params.get("discover"):
                client.discovers = None
                return {}
            wanted = [entry.get("type") for entry in params.get("filter") or [{"type": "page"}]]
            client.discovers = frozenset(str(kind) for kind in wanted if kind)
            for existing in list(self.targets.values()):
                if existing.type in client.discovers:
                    client.fire("Target.targetCreated", {"targetInfo": existing.info()}, None)
            return {}
        if method == "Target.closeTarget":
            if params.get("targetId") not in self.targets:
                raise RuntimeError("CDP error: No target with given id found")
            self.close_target(str(params["targetId"]))
            return {"success": True}
        if method == "Target.attachToTarget":
            attached = self.targets.get(params["targetId"])
            if attached is None:
                raise RuntimeError("CDP error: No target with given id found")
            sid = self.new_session(f"c{client.id}")
            attached.sessions[client.id] = sid
            return {"sessionId": sid}
        if method == "Target.createTarget":
            target_id = f"page-{len(self.targets) + 1}"
            while target_id in self.targets:
                target_id = f"{target_id}x"
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
        if method == "Network.streamResourceContent":
            return {"bufferedData": ""}
        if method == "Storage.getCookies":
            return {"cookies": [dict(cookie) for cookie in self.cookies]}
        if method == "DOMStorage.getDOMStorageItems":
            storage_id = params.get("storageId") or {}
            store = self.local_storage if storage_id.get("isLocalStorage") else self.session_storage
            items = store.get(str(storage_id.get("securityOrigin")), {})
            return {"entries": [[key, value] for key, value in items.items()]}
        if method == "Page.createIsolatedWorld":
            if params.get("worldName") == PLATFORM_WORLD:
                context = 13000 + len(self.platform_contexts) + 1
                self.platform_contexts.add(context)
                return {"executionContextId": context}
            if params.get("worldName") == COLOR_SCHEME_WORLD:
                context = 11000 + len(self.color_scheme_contexts) + 1
                self.color_scheme_contexts.add(context)
                return {"executionContextId": context}
            if params.get("worldName") == CLIENT_HINTS_WORLD:
                context = 9000 + len(self.client_hints_contexts) + 1
                self.client_hints_contexts.add(context)
                return {"executionContextId": context}
            if params.get("worldName") == DEFAULTS_WORLD:
                context = 7000 + len(self.defaults_contexts) + 1
                self.defaults_contexts.add(context)
                return {"executionContextId": context}
            if params.get("worldName") == SETTLE_WORLD:
                context = 5000 + len(self.settle_contexts) + 1
                self.settle_contexts.add(context)
                return {"executionContextId": context}
            if params.get("worldName") == MEDIA_WORLD:
                context = 15000 + len(self.media_contexts) + 1
                self.media_contexts.add(context)
                return {"executionContextId": context}
            self.isolated_worlds += 1
            return {"executionContextId": 1000 + self.isolated_worlds}
        if method == "Runtime.evaluate":
            if params.get("contextId") in self.platform_contexts:
                return {"result": {"type": "string", "value": self.browser_os}}
            if params.get("contextId") in self.color_scheme_contexts:
                return {"result": {"type": "string", "value": self.color_scheme}}
            if params.get("contextId") in self.client_hints_contexts:
                return {"result": {"type": "string", "value": json.dumps(self.client_hints)}}
            if params.get("contextId") in self.defaults_contexts:
                defaults = {"timezone": self.default_timezone, "locale": self.default_locale}
                return {"result": {"type": "string", "value": json.dumps(defaults)}}
            if params.get("contextId") in self.settle_contexts:
                self.settle_probes += 1
                return {"result": {"type": "object", "value": dict(self.settle_answer)}}
            if params.get("contextId") in self.media_contexts:
                if "framePath" in str(params.get("expression") or ""):
                    return {"result": {"type": "object", "value": {"found": False}}}
                self.media_reads += 1
                return {
                    "result": {"type": "object", "value": json.loads(json.dumps(self.media_answer))}
                }
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
