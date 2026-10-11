"""A deterministic local web site for recorder / archive / replay tests.

Shared by M29 (recording), M30 (replay) and later modules, so every live
test asks the same questions of the same pages. Three servers on
``127.0.0.1`` ephemeral ports:

- **main** — the site under test;
- **cross** — a second *site*, for an out-of-process iframe under site
  isolation, and — on the exact host ``localhost``, a secure context — the
  service-worker page;
- **ws** — a WebSocket echo server.

Two host modes:

- ``mapped=False``: main is ``http://127.0.0.1:P``, cross is
  ``http://localhost:P`` (``127.0.0.1`` and ``localhost`` are different
  sites), ws is ``ws://127.0.0.1:P``.
- ``mapped=True``: main is ``http://shop.example.test``, cross is
  ``http://ads.thirdparty.test``, ws is ``ws://ws.example.test``, each mapped
  to its server by :meth:`FixtureSite.chrome_args` —
  ``--host-resolver-rules=MAP … 127.0.0.1:P, …`` plus ``--no-proxy-server``
  (without it Chrome on macOS uses the system proxy and the rules map only
  the proxy host; S2a Q7). Pass them as the provider's ``extra_args``. These
  names are what M30's DNS blackhole makes unreachable, so a replay miss is
  a real miss. The service-worker page stays on ``http://localhost:P``.

Every scenario lives under ``/<scenario>/``, so a log entry belongs to
exactly one scenario. Two logs are the completeness oracle and the egress
counter: :attr:`FixtureSite.requests` (host, method, path, header names,
body sha256 — every HTTP request any server answered, WebSocket handshakes
included) and :meth:`FixtureSite.accepts` (TCP connections accepted per
server, preconnects included). Content is deterministic: no focused inputs
(no caret), no animation, no web fonts, ``<link rel=icon href="data:,">``
on every page so no favicon request is made.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from websockets.sync.server import Server as WebSocketServer
from websockets.sync.server import ServerConnection
from websockets.sync.server import serve as serve_websocket

#: A planted credential for the POST form scenario (privacy tests search for it).
FIXTURE_PASSWORD = "fixture-password-Zq81Lm"
#: A planted credential for the JSON POST scenario.
FIXTURE_TOKEN = "fixture-token-Vx27Kp"
#: Size of the large bodies — over ``browser.network``'s 5 MB inline tier.
LARGE_BODY_BYTES = 6 * 1024 * 1024
#: Size of the Range-served media resource.
VIDEO_BYTES = 300_000

SCENARIOS: tuple[str, ...] = (
    "redirect",
    "post_form",
    "json_post",
    "graphql",
    "oopif",
    "worker",
    "nested_worker",
    "service_worker",
    "popup",
    "large",
    "range_video",
    "websocket",
    "beacon",
)
#: M30's replay-only pages, kept apart so M29's scenario sweeps are unchanged:
#: ``clock`` shows the page's pinned clock and seeded randomness in a button;
#: ``canary`` fetches a host no recording can hold; ``strict`` makes one
#: request of each miss class to hosts a hermetic recording cannot reach
#: (a sub-frame document, a beacon, a subresource); ``egress`` is served by
#: IP literal and reaches the fixture servers only by IP literal — a
#: ``<link rel=preconnect>`` to cross, a script and a WebSocket — routes that
#: need no name mapping, so in a replay only the isolation's blackhole
#: (``MAP *`` covers IP literals, S2a Q2) cuts them, and a replay that let a
#: request through would reach the fixture.
REPLAY_SCENARIOS: tuple[str, ...] = ("clock", "canary", "strict", "egress")
#: M31's branch pages, kept apart for the same reason: a small shop flow whose
#: every step is an *index* action — a link (``open-form``), a text input
#: (``item``), a radio button (``size-large``), a link the page keeps pointing
#: at the form's state (``add-to-cart``) — so a branch restores it by
#: re-finding elements, never by coordinates. The result page's
#: ``mark`` button sets a session cookie and a ``localStorage`` entry from JS
#: (no request); the ``check`` page renders both on load — what a branch that
#: inherited another branch's browser state would show.
BRANCH_SCENARIOS: tuple[str, ...] = ("branch",)
#: The cookie and the ``localStorage`` key the ``mark`` button sets.
BRANCH_MARK = "branch_mark"
#: Hosts the replay-only pages ask for and no fixture server answers: a
#: hermetic recording (:meth:`FixtureSite.chrome_args` ``hermetic=True``)
#: records each as a request that failed, so a replay has nothing to serve.
CANARY_HOST = "live-only.example.test"
UNREACHABLE_HOSTS: tuple[str, ...] = (
    CANARY_HOST,
    "frames.example.test",
    "stats.example.test",
    "api.example.test",
    "gone.example.test",
)

_HTML = "text/html; charset=utf-8"
_JS = "application/javascript"
_JSON = "application/json"
_HEAD = '<meta charset="utf-8"><link rel="icon" href="data:,">'


@dataclass(frozen=True)
class LoggedRequest:
    """One request a fixture server answered."""

    #: ``main`` / ``cross`` / ``ws``.
    server: str
    #: The ``Host`` header as received.
    host: str
    method: str
    #: Path including any query string.
    path: str
    #: Request header names, lower-cased and sorted.
    header_names: tuple[str, ...]
    body_sha256: str | None
    body_length: int
    range: str | None = None

    @property
    def bare_path(self) -> str:
        return self.path.split("?", 1)[0]

    @property
    def scenario(self) -> str | None:
        head = self.bare_path.strip("/").split("/", 1)[0]
        known = head in SCENARIOS or head in REPLAY_SCENARIOS or head in BRANCH_SCENARIOS
        return head if known else None


def _large_js() -> bytes:
    filler = b"/*" + b"x" * (LARGE_BODY_BYTES - 64) + b"*/\n"
    return filler + b"window.largeStaticLoaded = true;\n"


def _large_bin() -> bytes:
    block = bytes(range(256))
    return block * (LARGE_BODY_BYTES // len(block))


def _video() -> bytes:
    block = bytes((i * 7) % 256 for i in range(1000))
    return (block * (VIDEO_BYTES // len(block) + 1))[:VIDEO_BYTES]


class _AcceptCountingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, role: str, site: FixtureSite) -> None:
        self.role = role
        self.site = site
        super().__init__(("127.0.0.1", 0), _Handler)

    def get_request(self) -> tuple[socket.socket, Any]:
        conn, addr = super().get_request()
        self.site._count_accept(self.role)
        return conn, addr


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _AcceptCountingServer

    def log_message(self, *_args: Any) -> None:
        return None

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        site = self.server.site
        site._log(
            LoggedRequest(
                server=self.server.role,
                host=self.headers.get("Host", ""),
                method=self.command,
                path=self.path,
                header_names=tuple(sorted(k.lower() for k in self.headers)),
                body_sha256=hashlib.sha256(body).hexdigest() if body else None,
                body_length=len(body),
                range=self.headers.get("Range"),
            )
        )
        site._respond(self, body)

    do_GET = _serve
    do_POST = _serve
    do_HEAD = _serve


class FixtureSite:
    """The fixture site; use as a context manager or call start()/close()."""

    MAIN_HOST = "shop.example.test"
    CROSS_HOST = "ads.thirdparty.test"
    WS_HOST = "ws.example.test"

    def __init__(self, *, mapped: bool = False) -> None:
        self.mapped = mapped
        self._lock = threading.Lock()
        self._requests: list[LoggedRequest] = []
        self._accepts: dict[str, int] = {"main": 0, "cross": 0, "ws": 0}
        self._main: _AcceptCountingServer | None = None
        self._cross: _AcceptCountingServer | None = None
        self._ws: WebSocketServer | None = None
        self._threads: list[threading.Thread] = []
        self._large_js = _large_js()
        self._large_bin = _large_bin()
        self._video = _video()
        self._route_table: dict[str, Callable[[_Handler, bytes], None]] | None = None

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> FixtureSite:
        self._main = _AcceptCountingServer("main", self)
        self._cross = _AcceptCountingServer("cross", self)
        self._ws = serve_websocket(
            self._echo,
            "127.0.0.1",
            0,
            process_request=self._log_handshake,
            compression=None,
        )
        # Count the TCP accept, not the handshake: a client may connect and
        # never send one — that is still egress. (Measured, Chrome 155: a
        # replayed page's WebSocket to 127.0.0.1, launched without isolation,
        # connects and closes before its request line.)
        handshake = self._ws.handler

        def count_then_handshake(sock: socket.socket, addr: Any) -> None:
            self._count_accept("ws")
            handshake(sock, addr)

        self._ws.handler = count_then_handshake
        for target in (
            lambda: self._main and self._main.serve_forever(poll_interval=0.02),
            lambda: self._cross and self._cross.serve_forever(poll_interval=0.02),
            self._ws.serve_forever,
        ):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def close(self) -> None:
        for server in (self._main, self._cross):
            if server is not None:
                server.shutdown()
                server.server_close()
        if self._ws is not None:
            self._ws.shutdown()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()

    def __enter__(self) -> FixtureSite:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── addresses ─────────────────────────────────────────────────────

    @property
    def main_port(self) -> int:
        assert self._main is not None, "FixtureSite not started"
        return self._main.server_address[1]

    @property
    def cross_port(self) -> int:
        assert self._cross is not None, "FixtureSite not started"
        return self._cross.server_address[1]

    @property
    def ws_port(self) -> int:
        assert self._ws is not None, "FixtureSite not started"
        return self._ws.socket.getsockname()[1]

    @property
    def main_origin(self) -> str:
        return f"http://{self.MAIN_HOST}" if self.mapped else f"http://127.0.0.1:{self.main_port}"

    @property
    def cross_origin(self) -> str:
        return f"http://{self.CROSS_HOST}" if self.mapped else f"http://localhost:{self.cross_port}"

    @property
    def ws_origin(self) -> str:
        return f"ws://{self.WS_HOST}" if self.mapped else f"ws://127.0.0.1:{self.ws_port}"

    @property
    def ip_origin(self) -> str:
        """The main server by IP literal — reachable without a name mapping, in both modes."""
        return f"http://127.0.0.1:{self.main_port}"

    @property
    def sw_origin(self) -> str:
        """Exact ``localhost`` — a secure context over plain http, in both modes."""
        return f"http://localhost:{self.cross_port}"

    def chrome_args(self, *, hermetic: bool = False) -> list[str]:
        """Switches that make the mapped hostnames reach the fixture servers.

        *hermetic* (M30's recordings) resolves every other name to nothing
        as well (``MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1``
        after the fixture's own rules — the first match wins, S2a Q2), so a
        recording never leaves the machine. Loopback stays reachable — exact
        ``localhost`` and the IP literal ``127.0.0.1`` (which ``MAP *`` would
        blackhole too), where the fixture servers are: the ``egress`` page
        links them by IP literal. A replay's isolation has no such exclusion.
        """
        if not self.mapped:
            return []
        rules = [
            f"MAP {self.MAIN_HOST} 127.0.0.1:{self.main_port}",
            f"MAP {self.CROSS_HOST} 127.0.0.1:{self.cross_port}",
            f"MAP {self.WS_HOST} 127.0.0.1:{self.ws_port}",
        ]
        if hermetic:
            rules.append("MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1")
        return [f"--host-resolver-rules={', '.join(rules)}", "--no-proxy-server"]

    def url(self, scenario: str) -> str:
        """The URL a test navigates to for ``scenario``."""
        entry = {
            "redirect": "/redirect/hop1",
            "service_worker": "/service_worker/page.html",
            "popup": "/popup/opener.html",
        }.get(scenario, f"/{scenario}/page.html")
        known = SCENARIOS + REPLAY_SCENARIOS + BRANCH_SCENARIOS
        if scenario not in known:
            raise KeyError(f"unknown fixture scenario {scenario!r}; known: {known}")
        origin = {"service_worker": self.sw_origin, "egress": self.ip_origin}.get(
            scenario, self.main_origin
        )
        return origin + entry

    @staticmethod
    def expected(scenario: str) -> list[str]:
        """Every path (without query) ``scenario`` makes the browser request."""
        paths = {
            "redirect": ["/redirect/hop1", "/redirect/hop2", "/redirect/landing.html"],
            "post_form": ["/post_form/page.html", "/post_form/submit"],
            "json_post": ["/json_post/page.html", "/json_post/api"],
            "graphql": ["/graphql/page.html", "/graphql/endpoint", "/graphql/endpoint"],
            "oopif": [
                "/oopif/page.html",
                "/oopif/frame.html",
                "/oopif/frame.js",
                "/oopif/frame-data.json",
            ],
            "worker": ["/worker/page.html", "/worker/worker.js", "/worker/data.json"],
            "nested_worker": [
                "/nested_worker/page.html",
                "/nested_worker/outer.js",
                "/nested_worker/inner.js",
                "/nested_worker/data.json",
            ],
            "service_worker": [
                "/service_worker/page.html",
                "/service_worker/sw.js",
                "/service_worker/install.json",
                "/service_worker/through-sw.json",
            ],
            "popup": [
                "/popup/opener.html",
                "/popup/popup.html",
                "/popup/popup.js",
                "/popup/data.json",
            ],
            "large": ["/large/page.html", "/large/static.js", "/large/data.bin"],
            "range_video": ["/range_video/page.html", "/range_video/clip.webm"],
            "websocket": ["/websocket/page.html", "/websocket/echo"],
            "beacon": ["/beacon/page.html", "/beacon/collect"],
        }
        if scenario not in paths:
            raise KeyError(f"unknown fixture scenario {scenario!r}; known: {SCENARIOS}")
        return list(paths[scenario])

    @staticmethod
    def expected_again(scenario: str) -> list[str]:
        """Every path a later visit to ``scenario`` in the same browser requests.

        The same as :meth:`expected`, except that the service worker the first
        visit installed stays installed: its install-time request is not made
        again, and its script is fetched only by the browser's own update
        check, after the page's requests and often after the step (measured,
        Chrome 155) — so nothing waits for it.
        """
        again = {"service_worker": ["/service_worker/page.html", "/service_worker/through-sw.json"]}
        return list(again.get(scenario) or FixtureSite.expected(scenario))

    # ── logs ──────────────────────────────────────────────────────────

    @property
    def requests(self) -> list[LoggedRequest]:
        with self._lock:
            return list(self._requests)

    def requests_for(self, scenario: str) -> list[LoggedRequest]:
        return [r for r in self.requests if r.scenario == scenario]

    def accepts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._accepts)

    def _log(self, entry: LoggedRequest) -> None:
        with self._lock:
            self._requests.append(entry)

    def _count_accept(self, role: str) -> None:
        with self._lock:
            self._accepts[role] += 1

    # ── WebSocket ─────────────────────────────────────────────────────

    def _log_handshake(self, connection: ServerConnection, request: Any) -> None:
        self._log(
            LoggedRequest(
                server="ws",
                host=request.headers.get("Host", ""),
                method="GET",
                path=request.path,
                header_names=tuple(sorted(k.lower() for k in request.headers)),
                body_sha256=None,
                body_length=0,
            )
        )
        return None

    @staticmethod
    def _echo(connection: ServerConnection) -> None:
        for message in connection:
            connection.send(message)

    # ── content ───────────────────────────────────────────────────────

    def _page(self, title: str, body: str) -> bytes:
        return (
            f"<!doctype html><html><head>{_HEAD}<title>{title}</title></head>"
            f"<body>{body}</body></html>"
        ).encode()

    def _routes(self) -> dict[str, Callable[[_Handler, bytes], None]]:
        def static(content: bytes, ctype: str) -> Callable[[_Handler, bytes], None]:
            return lambda handler, _body: self._send(handler, 200, content, ctype)

        def json_echo(handler: _Handler, body: bytes) -> None:
            reply = {"ok": True, "received_bytes": len(body)}
            self._send(handler, 200, json.dumps(reply, sort_keys=True).encode(), _JSON)

        cross = self.cross_origin
        ws = self.ws_origin
        ip_main = self.ip_origin
        ip_cross = f"http://127.0.0.1:{self.cross_port}"
        ip_ws = f"ws://127.0.0.1:{self.ws_port}"
        return {
            # redirect: 302 → 301 → 200
            "/redirect/hop1": lambda h, _b: self._redirect(h, 302, "/redirect/hop2"),
            "/redirect/hop2": lambda h, _b: self._redirect(h, 301, "/redirect/landing.html"),
            "/redirect/landing.html": static(
                self._page("redirect", "<p>landed after 302 and 301</p>"), _HTML
            ),
            # a form POST carrying a password, submitted on load (a navigation)
            "/post_form/page.html": static(
                self._page(
                    "post form",
                    '<form method="POST" action="/post_form/submit">'
                    '<input name="username" value="alice">'
                    f'<input type="password" name="password" value="{FIXTURE_PASSWORD}">'
                    "</form><script>document.forms[0].submit()</script>",
                ),
                _HTML,
            ),
            "/post_form/submit": static(self._page("submitted", "<p>form received</p>"), _HTML),
            # a JSON POST from fetch
            "/json_post/page.html": static(
                self._page(
                    "json post",
                    "<p>json</p><script>fetch('/json_post/api', {method: 'POST', "
                    "headers: {'Content-Type': 'application/json'}, body: JSON.stringify("
                    f"{{query: 'shoes', page: 2, token: '{FIXTURE_TOKEN}'}})}})"
                    ".then(r => r.json()).then(j => { document.title = 'json ' + j.ok; });"
                    "</script>",
                ),
                _HTML,
            ),
            "/json_post/api": json_echo,
            # GraphQL: one single and one batched operation
            "/graphql/page.html": static(
                self._page(
                    "graphql",
                    "<p>graphql</p><script>"
                    "const q = {operationName: 'Item', query: 'query Item($id: Int) "
                    "{ item(id: $id) { name } }', variables: {id: 1}};"
                    "const post = body => fetch('/graphql/endpoint', {method: 'POST', "
                    "headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});"
                    "post(q).then(() => post([q, {operationName: 'Other', "
                    "query: 'query Other { other }', variables: {}}]))"
                    ".then(() => { document.title = 'graphql done'; });"
                    "</script>",
                ),
                _HTML,
            ),
            "/graphql/endpoint": json_echo,
            # a cross-site iframe (out of process under site isolation)
            "/oopif/page.html": static(
                self._page(
                    "oopif",
                    f'<p>outer</p><iframe src="{cross}/oopif/frame.html" '
                    'style="width:300px;height:120px;border:0"></iframe>',
                ),
                _HTML,
            ),
            "/oopif/frame.html": static(
                self._page(
                    "frame",
                    '<p>inner</p><script src="/oopif/frame.js"></script>'
                    "<script>fetch('/oopif/frame-data.json')</script>",
                ),
                _HTML,
            ),
            "/oopif/frame.js": static(b"window.frameScript = 1;\n", _JS),
            "/oopif/frame-data.json": static(b'{"frame": 1}', _JSON),
            # a dedicated worker that fetches
            "/worker/page.html": static(
                self._page(
                    "worker",
                    "<p>worker</p><script>const w = new Worker('/worker/worker.js');"
                    "w.onmessage = e => { document.title = 'worker ' + e.data; };</script>",
                ),
                _HTML,
            ),
            "/worker/worker.js": static(
                b"fetch('/worker/data.json').then(r => r.json()).then(j => postMessage(j.w));\n",
                _JS,
            ),
            "/worker/data.json": static(b'{"w": 1}', _JSON),
            # a worker that starts a worker (bypasses Fetch: a known limitation)
            "/nested_worker/page.html": static(
                self._page(
                    "nested worker",
                    "<p>nested</p><script>new Worker('/nested_worker/outer.js');</script>",
                ),
                _HTML,
            ),
            "/nested_worker/outer.js": static(b"new Worker('/nested_worker/inner.js');\n", _JS),
            "/nested_worker/inner.js": static(b"fetch('/nested_worker/data.json');\n", _JS),
            "/nested_worker/data.json": static(b'{"nested": 1}', _JSON),
            # a service worker (served on exact localhost: a secure context)
            "/service_worker/page.html": static(
                self._page(
                    "service worker",
                    "<p>sw</p><script>"
                    "navigator.serviceWorker.register('/service_worker/sw.js', "
                    "{scope: '/service_worker/'})"
                    ".then(() => navigator.serviceWorker.ready)"
                    ".then(() => fetch('/service_worker/through-sw.json'))"
                    ".then(() => { document.title = 'sw done'; })"
                    ".catch(e => { document.title = 'sw blocked: ' + e.name; });"
                    "</script>",
                ),
                _HTML,
            ),
            "/service_worker/sw.js": static(
                b"self.addEventListener('install', e => "
                b"e.waitUntil(fetch('/service_worker/install.json')));\n"
                b"self.addEventListener('activate', e => e.waitUntil(clients.claim()));\n"
                b"self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));\n",
                _JS,
            ),
            "/service_worker/install.json": static(b'{"install": 1}', _JSON),
            "/service_worker/through-sw.json": static(b'{"via": "sw"}', _JSON),
            # a popup opened on load (Chrome's popup blocker is off by default
            # in local_chrome; tests may also open it with a user gesture)
            "/popup/opener.html": static(
                self._page(
                    "opener", "<p>opener</p><script>window.open('/popup/popup.html');</script>"
                ),
                _HTML,
            ),
            "/popup/popup.html": static(
                self._page(
                    "popup",
                    '<p>popup</p><script src="/popup/popup.js"></script>'
                    "<script>fetch('/popup/data.json')</script>",
                ),
                _HTML,
            ),
            "/popup/popup.js": static(b"window.popupScript = 1;\n", _JS),
            "/popup/data.json": static(b'{"popup": 1}', _JSON),
            # bodies over the 5 MB inline tier: a static script and an XHR
            "/large/page.html": static(
                self._page(
                    "large",
                    '<p>large</p><script src="/large/static.js"></script><script>'
                    "const x = new XMLHttpRequest(); x.open('GET', '/large/data.bin');"
                    "x.onload = () => { document.title = 'large ' + x.response.length; };"
                    "x.send();</script>",
                ),
                _HTML,
            ),
            "/large/static.js": static(self._large_js, _JS),
            "/large/data.bin": static(self._large_bin, "application/octet-stream"),
            # media fetched with Range (a hand-written 206)
            "/range_video/page.html": static(
                self._page(
                    "range video",
                    '<video src="/range_video/clip.webm" preload="auto" muted '
                    'style="width:160px;height:90px"></video>',
                ),
                _HTML,
            ),
            "/range_video/clip.webm": self._range,
            # a WebSocket round trip
            "/websocket/page.html": static(
                self._page(
                    "websocket",
                    "<p>ws</p><script>"
                    f"const s = new WebSocket('{ws}/websocket/echo');"
                    "s.onopen = () => s.send('hello fixture');"
                    "s.onmessage = e => { document.title = 'ws ' + e.data; s.close(); };"
                    "</script>",
                ),
                _HTML,
            ),
            # an analytics beacon
            "/beacon/page.html": static(
                self._page(
                    "beacon",
                    "<p>beacon</p><script>"
                    "navigator.sendBeacon('/beacon/collect', 'event=view&page=beacon');"
                    "</script>",
                ),
                _HTML,
            ),
            "/beacon/collect": lambda h, _b: self._send(h, 204, b"", "text/plain"),
            # ── M30's replay-only pages ──
            # the page's clock and randomness, where a DOM signature sees them
            "/clock/page.html": static(
                self._page(
                    "clock",
                    '<button id="clock" type="button">clock</button><script>'
                    "document.getElementById('clock').textContent = "
                    "new Date().toISOString() + ' ' + Math.random().toFixed(8);"
                    "</script>",
                ),
                _HTML,
            ),
            # a host no recording can hold
            "/canary/page.html": static(
                self._page(
                    "canary",
                    "<p>canary</p><script>"
                    f"fetch('http://{CANARY_HOST}/canary.json')"
                    ".then(() => { document.title = 'canary answered'; })"
                    ".catch(() => { document.title = 'canary failed'; });"
                    "</script>",
                ),
                _HTML,
            ),
            # one request of each miss class, to hosts a hermetic recording cannot reach
            "/strict/page.html": static(
                self._page(
                    "strict",
                    "<p>strict</p>"
                    '<iframe src="http://frames.example.test/sub.html" '
                    'style="width:200px;height:60px;border:0"></iframe><script>'
                    "navigator.sendBeacon('http://stats.example.test/collect', 'e=1');"
                    "fetch('http://api.example.test/data.json').catch(() => {});"
                    "</script>",
                ),
                _HTML,
            ),
            # served by IP literal; reaches the fixture servers by IP literal: a
            # preconnect to cross (never a request, never paused), a script
            # (paused at the Request stage) and a WebSocket (never paused);
            # nothing on the page shows the socket's fate
            "/egress/page.html": static(
                self._page(
                    "egress",
                    f'<link rel="preconnect" href="{ip_cross}">'
                    '<button id="ip" type="button">pending</button>'
                    f'<script src="{ip_main}/egress/ip.js"></script>'
                    f"<script>new WebSocket('{ip_ws}/egress/echo');</script>",
                ),
                _HTML,
            ),
            "/egress/ip.js": static(
                b"document.getElementById('ip').textContent = 'served by IP literal';\n", _JS
            ),
            # ── M31's branch pages: every step an index action ──
            "/branch/page.html": static(
                self._page(
                    "kettle shop",
                    "<h1>Kettle shop</h1><p>Put a kettle in the cart.</p>"
                    '<p><a id="open-form" href="/branch/form.html">Open the order form</a></p>'
                    '<p><button id="help" type="button">Help</button></p>',
                ),
                _HTML,
            ),
            "/branch/form.html": static(
                self._page(
                    "order form",
                    '<p><label for="item">Item</label> '
                    '<input id="item" name="item" type="text" size="20"></p>'
                    "<p>Size: "
                    '<label><input id="size-small" type="radio" name="size" value="small" '
                    "checked> Small</label> "
                    '<label><input id="size-large" type="radio" name="size" value="large">'
                    " Large</label></p>"
                    '<p><a id="add-to-cart" href="/branch/result.html?item=&amp;size=small">'
                    "Add to cart</a></p>"
                    # A link the form keeps pointing at its state, not a form
                    # submission: clicking a submit button leaves the next
                    # observation's capture hanging (an open issue of M31's report).
                    "<script>"
                    "const link = document.getElementById('add-to-cart');"
                    "const update = () => { link.href = '/branch/result.html?item=' + "
                    "encodeURIComponent(document.getElementById('item').value) + '&size=' + "
                    "document.querySelector('input[name=size]:checked').value; };"
                    "document.getElementById('item').addEventListener('input', update);"
                    "for (const radio of document.querySelectorAll('input[name=size]')) "
                    "radio.addEventListener('change', update);"
                    "</script>",
                ),
                _HTML,
            ),
            "/branch/result.html": static(
                self._page(
                    "cart",
                    '<p>In the cart: <span id="cart">nothing</span></p>'
                    '<p><button id="mark" type="button">Remember me</button></p>'
                    '<p><a id="check" href="/branch/check.html">Check the saved state</a></p>'
                    "<script>"
                    "const q = new URLSearchParams(location.search);"
                    "document.getElementById('cart').textContent = "
                    "(q.get('size') || '?') + ' ' + (q.get('item') || '?');"
                    "document.getElementById('mark').onclick = () => {"
                    f"document.cookie = '{BRANCH_MARK}=remembered; path=/';"
                    f"localStorage.setItem('{BRANCH_MARK}', 'remembered');"
                    "document.getElementById('mark').textContent = 'Remembered';"
                    "};"
                    "</script>",
                ),
                _HTML,
            ),
            "/branch/check.html": static(
                self._page(
                    "saved state",
                    '<p id="cookie">cookie: ?</p><p id="storage">storage: ?</p>'
                    '<p><a id="home" href="/branch/page.html">Back to the shop</a></p>'
                    "<script>"
                    "document.getElementById('cookie').textContent = "
                    "'cookie: ' + (document.cookie || '(none)');"
                    "document.getElementById('storage').textContent = "
                    f"'storage: ' + (localStorage.getItem('{BRANCH_MARK}') || '(none)');"
                    "</script>",
                ),
                _HTML,
            ),
        }

    def _respond(self, handler: _Handler, body: bytes) -> None:
        if self._route_table is None:
            self._route_table = self._routes()  # ports are known once serving
        route = self._route_table.get(handler.path.split("?", 1)[0])
        if route is None:
            self._send(handler, 404, b"not found", "text/plain")
            return
        route(handler, body)

    @staticmethod
    def _send(handler: _Handler, status: int, content: bytes, ctype: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(content)

    @staticmethod
    def _redirect(handler: _Handler, status: int, location: str) -> None:
        handler.send_response(status)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()

    def _range(self, handler: _Handler, _body: bytes) -> None:
        total = len(self._video)
        header = handler.headers.get("Range")
        if not header or not header.startswith("bytes="):
            self._send(handler, 200, self._video, "video/webm")
            return
        start_text, _, end_text = header[len("bytes=") :].partition("-")
        start = int(start_text or 0)
        end = min(int(end_text), total - 1) if end_text else total - 1
        if start >= total or start > end:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{total}")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        chunk = self._video[start : end + 1]
        handler.send_response(206)
        handler.send_header("Content-Type", "video/webm")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        handler.send_header("Content-Length", str(len(chunk)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(chunk)
