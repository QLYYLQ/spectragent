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
  the proxy host; measured, arch §4.14). Pass them as the provider's ``extra_args``. These
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

import gzip
import hashlib
import io
import json
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl

from PIL import Image
from websockets.sync.server import Server as WebSocketServer
from websockets.sync.server import ServerConnection
from websockets.sync.server import serve as serve_websocket

#: A planted credential for the POST form scenario (privacy tests search for it).
FIXTURE_PASSWORD = "fixture-password-Zq81Lm"
#: A planted credential for the JSON POST scenario.
FIXTURE_TOKEN = "fixture-token-Vx27Kp"
#: Size of the large bodies — over ``browser.network``'s 5 MB inline tier, so
#: streamed while the page reads them.
LARGE_BODY_BYTES = 6 * 1024 * 1024
#: Size of the ``strict`` page's huge body — over the 64 MiB limit, the
#: replay's default limit for one body, so recorded ``too_large``. Streamed
#: from a repeated block, never held whole.
HUGE_BODY_BYTES = 65 * 1024 * 1024
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
#: (a sub-frame document, a beacon, a subresource), and fetches one body over
#: the 64 MiB limit (:data:`HUGE_BODY_BYTES`, recorded without it); ``egress`` is served by
#: IP literal and reaches the fixture servers only by IP literal — a
#: ``<link rel=preconnect>`` to cross, a script and a WebSocket — routes that
#: need no name mapping, so in a replay only the isolation's blackhole
#: (``MAP *`` covers IP literals, measured, arch §4.14) cuts them, and a replay that let a
#: request through would reach the fixture; ``localhost`` is the same page on
#: exact ``localhost`` — served there, its script, a ``<link rel=preconnect>``
#: to cross and a WebSocket all by ``localhost`` — the name the isolation
#: once left out (``EXCLUDE localhost``): a recording made against a local
#: site, which a replay serves from the archive with the fixture still up.
REPLAY_SCENARIOS: tuple[str, ...] = ("clock", "canary", "strict", "egress", "localhost")
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
#: M32's occlusion page, kept apart for the same reason: a sticky header
#: (``nav``, 100 CSS px tall) over links the page has scrolled underneath it
#: (``window.scrollTo(0, 500)`` on load, 1280x720 viewport) — ``covered``
#: lies wholly under the header, ``partial`` has its centre under it and its
#: lower part visible, ``clear`` is uncovered, ``agree`` is a checkbox its own
#: label's text covers (a click there still reaches it), ``below`` is past
#: the bottom of the viewport, and a same-process iframe holds ``inside``.
HIT_TEST_SCENARIOS: tuple[str, ...] = ("sticky",)
#: The scroll offset the ``sticky`` page sets on load, in CSS px.
STICKY_SCROLL_Y = 500
#: M33's form pages, kept apart for the same reason: a navigation a *page*
#: starts in answer to an input event, which the action does not wait for (the
#: observation does, §4.14). ``get.html`` and ``post.html`` hold a prefilled
#: ``item`` field and an ``add`` submit button; ``enter.html`` holds one
#: ``query`` field and no button (Enter submits it, implicit submission);
#: ``result.html`` answers both methods, echoing the received fields.
FORM_SCENARIOS: tuple[str, ...] = ("form_submit",)
#: M33's metrics rehearsal, kept apart for the same reason: three *sites* (each
#: its own mapped host, so the report's per-site statistics have something to
#: tell apart), driven by UI-TARS coordinates — every control is absolutely
#: positioned (:data:`FLYWHEEL_BOXES`):
#:
#: - ``flywheel_shop`` on ``shop.example.test``: a search field and a GET
#:   submit button (``start.html``), the results with a product link and a POST
#:   "quick add" form the recording never uses (``search.html?q=``), the product
#:   page, whose script fetches ``recommend.json?session=<performance.timeOrigin>``
#:   (pinned fixes Date and Math.random, not the time origin: every replay asks
#:   another URL — a subresource near miss), and ``login.html``, whose script
#:   assembles credentials at run time and sends them every way the framework
#:   promises not to record (an ``Authorization`` header, an urlencoded POST
#:   ``password``, a password field's value) — no response byte holds them.
#:   Not through ``document.cookie``: ``storage.json`` stores cookie values as
#:   they are (architecture §5.6), so a cookie-borne token is on disk by design;
#: - ``flywheel_drift`` on ``drift.example.test``: a button whose click
#:   navigates the main frame to ``next.html?visit=<performance.timeOrigin>``,
#:   synchronously — a replay asks a URL the recording never fetched;
#: - ``flywheel_blocked`` on ``blocked.example.test``: a 403 with Cloudflare's
#:   challenge marks (``server: cloudflare``, ``cf-ray``, ``cf-mitigated``).
FLYWHEEL_SCENARIOS: tuple[str, ...] = ("flywheel_shop", "flywheel_drift", "flywheel_blocked")
#: Tabs a page opens (§4.14), kept apart for the same reason. ``page.html`` holds
#: one control per way of opening a tab (:data:`NEW_TAB_BOXES`): plain
#: ``target=_blank`` links (no ``rel``, ``rel=noopener``, ``rel=opener``, one to
#: the other site, one whose document answers after 2 s, one plain for the
#: replay) and ``window.open`` buttons (keeping the opener, ``noopener``, to the
#: other site). Each opens ``target.html``, whose first script reports what the
#: document sees (:data:`NEW_TAB_PROBE`) to ``/new_tab/report`` and, as asked
#: by its query, moves to a second document, starts a dedicated worker and
#: embeds a frame from the other site that report too. ``owners.html`` holds
#: two frames from the other site (the second added after 1 s) and a button
#: opening a same-site popup; ``owner.html`` reports its time zone and locale
#: to ``/new_tab/owner_report`` every 250 ms — what a frame reads once the frame
#: that set its process's overrides is gone. For following a new tab (§4.14
#: "页面新开的标签页"): the ``closes`` link opens a ``target.html`` that closes
#: itself :data:`NEW_TAB_CLOSES_AFTER_MS` after it loads, and ``timer.html``
#: opens a tab on its own :data:`NEW_TAB_TIMER_MS` after it loads, with no user
#: activation.
NEW_TAB_SCENARIOS: tuple[str, ...] = ("new_tab",)
#: When the ``closes`` link's tab closes itself, after it loads.
NEW_TAB_CLOSES_AFTER_MS = 1500
#: When ``timer.html`` opens its tab, after it loads.
NEW_TAB_TIMER_MS = 1500
#: Where ``new_tab/page.html`` puts its controls: ``(left, top, width, height)`` in CSS px.
NEW_TAB_BOXES: dict[str, tuple[int, int, int, int]] = {
    name: (20, 20 + 50 * i, 400, 36)
    for i, name in enumerate(
        (
            "blank",
            "blank_noopener",
            "blank_opener",
            "blank_cross_site",
            "window_open",
            "window_open_noopener",
            "window_open_cross_site",
            "slow",
            "replay",
            "closes",
        )
    )
}
#: Keys af sends (architecture §4.13), kept apart for the same reason. ``page.html``
#: holds a text field in a GET form with no button (``query``: Enter submits it
#: natively, implicit submission, to ``result.html``), a text field whose own
#: keydown handler acts on Enter the way a React search box does (``react``: it
#: checks ``key`` and ``keyCode`` and prevents the submission) and a textarea
#: (``notes``), at :data:`KEYS_BOXES`; a capture-phase listener keeps every
#: keydown, keypress and keyup in ``window.__keys``. ``result.html`` echoes the
#: query.
KEYS_SCENARIOS: tuple[str, ...] = ("keys",)
#: Where ``keys/page.html`` puts its fields: ``(left, top, width, height)`` in CSS px.
KEYS_BOXES: dict[str, tuple[int, int, int, int]] = {
    "query": (20, 20, 320, 32),
    "react": (20, 80, 320, 32),
    "notes": (20, 140, 320, 64),
}
#: The browser's client hints (architecture §4.14), kept apart for the same
#: reason: ``page.html``'s script writes what ``navigator.userAgentData`` says
#: (brands with versions, platform, mobile) into the page and reports it to
#: ``/client_hints/report``. ``navigator.userAgentData`` exists in secure
#: contexts only, and Chrome sends ``Sec-CH-UA`` to them only: serve it from an
#: unmapped site (``127.0.0.1``).
CLIENT_HINTS_SCENARIOS: tuple[str, ...] = ("client_hints",)
#: JSONP whose callback names differ between a recording and its replay
#: (architecture §4.14 "JSONP 回调名"), kept apart for the same reason.
#: ``page.html`` names its two callbacks after ``performance.timeOrigin`` (the
#: real clock, which pinned leaves alone, so a replay never asks the recording's
#: names) and loads two JSONP scripts: ``hot.js?callback=jQuery<µs>_1``, the
#: jQuery shape the built-in normalisation drops from the match key, answered
#: ``NAME({…});`` as ``application/json``; and ``ad.js?cb=ad_<µs in hex>``, a
#: name with a random part that only a site rule ignoring ``cb`` lets match,
#: answered in Express's wrapper (``/**/ typeof NAME === 'function' &&
#: NAME({…});``) as ``text/html``. Each callback adds one button per item
#: (:data:`JSONP_ITEMS`) to its box; a callback that never runs adds none. The
#: answers echo the name they are asked for, as a JSONP server does.
JSONP_SCENARIOS: tuple[str, ...] = ("jsonp",)
#: The buttons each JSONP answer adds, by the box (and the endpoint) they belong to.
JSONP_ITEMS: dict[str, tuple[str, ...]] = {"hot": ("first", "second", "third"), "ad": ("banner",)}
#: Frames the agent never sees (architecture §4.14 ``browser.frames``), kept apart
#: for the same reason. ``page.html`` holds one button, ``paint``, at
#: :data:`FRAMES_PAINT_BOX`; its click handler paints the whole page
#: :data:`FRAMES_PAINTED` at once (no animation, no request), so a screenshot
#: taken before the click and one taken after it differ at every pixel off the
#: button — :data:`FRAMES_PROBE` is white before and painted after.
FRAMES_SCENARIOS: tuple[str, ...] = ("frames",)
#: Where ``frames/page.html`` puts its button: ``(left, top, width, height)`` in CSS px.
FRAMES_PAINT_BOX: tuple[int, int, int, int] = (40, 40, 300, 80)
#: The colour the click paints the page.
FRAMES_PAINTED: tuple[int, int, int] = (255, 120, 0)
#: A point far from the button, in CSS px of the viewport.
FRAMES_PROBE: tuple[int, int] = (900, 500)
#: Documents a recording must keep and a replay must answer as recorded
#: (architecture §4.14 ``browser.network`` and "服务"), kept apart for the same
#: reason. ``slow_document/page.html`` is bilibili's search results: its
#: headers come at once and its body — GBK, gzip, no ``Content-Length`` —
#: :data:`SLOW_DOCUMENT_DELAY_S` later, past a short ``body_read_timeout_s``;
#: its ``again`` link (:data:`SLOW_DOCUMENT_BOXES`) opens it once more in a
#: new tab, the way bilibili opened its results.
DOCUMENT_SCENARIOS: tuple[str, ...] = ("slow_document",)
#: How long ``slow_document/page.html`` holds back the rest of its body.
SLOW_DOCUMENT_DELAY_S = 1.0
#: The text ``slow_document/page.html`` shows, sent GBK-encoded.
SLOW_DOCUMENT_TEXT = "搜索结果：慢的文档"
#: Where ``slow_document/page.html`` puts its links: ``(left, top, width, height)`` in CSS px.
SLOW_DOCUMENT_BOXES: dict[str, tuple[int, int, int, int]] = {
    "again": (20, 120, 300, 40),
}
#: What the first script of every ``new_tab`` document, worker and frame
#: reports: the shim's mark, the clock, the seeded randomness, time zone,
#: locale, language, user agent and brands, reduced motion — and whether any
#: other script ran before it.
NEW_TAB_PROBE = (
    "(function () {"
    " const g = globalThis; const o = Intl.DateTimeFormat().resolvedOptions();"
    " const doc = typeof document === 'object' ? document : null;"
    " return {"
    " shim: !!g[Symbol.for('af.replayability.pinned')],"
    " dates: [Date.now(), Date.now(), new Date().getTime()],"
    " random: [Math.random(), Math.random(), Math.random()],"
    " tz: o.timeZone || null, locale: o.locale || null,"
    " language: navigator.language, ua: navigator.userAgent,"
    " brands: navigator.userAgentData ? navigator.userAgentData.brands.map(b => b.brand) : null,"
    " reduced: typeof matchMedia === 'function'"
    " ? matchMedia('(prefers-reduced-motion: reduce)').matches : null,"
    " scripts: doc ? doc.scripts.length : null"
    " }; })()"
)
#: Request headers whose values the log keeps (:attr:`LoggedRequest.headers`).
LOGGED_HEADER_VALUES = (
    "accept-language",
    "user-agent",
    "cache-control",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
)
#: Where the flywheel pages put their controls: ``(left, top, width, height)`` in CSS px.
FLYWHEEL_BOXES: dict[str, tuple[int, int, int, int]] = {
    "query": (40, 100, 300, 32),
    "search": (360, 100, 100, 32),
    "product": (40, 160, 240, 32),
    "quick_add": (300, 160, 160, 32),
    "drift": (40, 100, 200, 40),
}
#: The pieces ``login.html`` joins at run time; the joined values are the planted credentials.
FLYWHEEL_PASSWORD_PARTS: tuple[str, ...] = ("fw", "pw", "Qx41", "Lm")
FLYWHEEL_TOKEN_PARTS: tuple[str, ...] = ("tok", "9Zr", "Kp27", "wq81")
FLYWHEEL_PASSWORD = "-".join(FLYWHEEL_PASSWORD_PARTS)
FLYWHEEL_TOKEN = "_".join(FLYWHEEL_TOKEN_PARTS)
#: Cloudflare's marks on the blocked site's answer.
FLYWHEEL_CHALLENGE_HEADERS: tuple[tuple[str, str], ...] = (
    ("Server", "cloudflare"),
    ("CF-RAY", "8a1b2c3d4e5f6a7b-AMS"),
    ("cf-mitigated", "challenge"),
)
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
#: A callback name the JSONP endpoints accept: one JavaScript identifier.
_JSONP_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


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
    #: ``time.monotonic()`` when the request line arrived.
    received_at: float = 0.0
    #: The values of :data:`LOGGED_HEADER_VALUES` the request carried (lower-cased names).
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def bare_path(self) -> str:
        return self.path.split("?", 1)[0]

    @property
    def query(self) -> dict[str, str]:
        return dict(parse_qsl(self.path.partition("?")[2], keep_blank_values=True))

    def header(self, name: str) -> str | None:
        """A logged header value (:data:`LOGGED_HEADER_VALUES`), or ``None`` when it was not sent."""
        return dict(self.headers).get(name.lower())

    @property
    def scenario(self) -> str | None:
        head = self.bare_path.strip("/").split("/", 1)[0]
        known = (
            head in SCENARIOS
            or head in REPLAY_SCENARIOS
            or head in BRANCH_SCENARIOS
            or head in HIT_TEST_SCENARIOS
            or head in FORM_SCENARIOS
            or head in FLYWHEEL_SCENARIOS
            or head in NEW_TAB_SCENARIOS
            or head in KEYS_SCENARIOS
            or head in CLIENT_HINTS_SCENARIOS
            or head in JSONP_SCENARIOS
            or head in FRAMES_SCENARIOS
            or head in DOCUMENT_SCENARIOS
        )
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
        received_at = time.monotonic()
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
                received_at=received_at,
                headers=tuple(
                    (name, str(self.headers[name]))
                    for name in LOGGED_HEADER_VALUES
                    if self.headers.get(name) is not None
                ),
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
    #: The flywheel rehearsal's other two sites, served by the main server.
    DRIFT_HOST = "drift.example.test"
    BLOCKED_HOST = "blocked.example.test"

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

    @property
    def localhost_origin(self) -> str:
        """The main server on exact ``localhost`` — reachable without a name mapping, in both modes."""
        return f"http://localhost:{self.main_port}"

    def chrome_args(self, *, hermetic: bool = False) -> list[str]:
        """Switches that make the mapped hostnames reach the fixture servers.

        *hermetic* (M30's recordings) resolves every other name to nothing
        as well (``MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1``
        after the fixture's own rules — the first match wins, measured, arch §4.14), so a
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
            f"MAP {self.DRIFT_HOST} 127.0.0.1:{self.main_port}",
            f"MAP {self.BLOCKED_HOST} 127.0.0.1:{self.main_port}",
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
            "form_submit": "/form_submit/get.html",
            "flywheel_shop": "/flywheel_shop/start.html",
            "flywheel_drift": "/flywheel_drift/start.html",
            "flywheel_blocked": "/flywheel_blocked/start.html",
        }.get(scenario, f"/{scenario}/page.html")
        known = (
            SCENARIOS
            + REPLAY_SCENARIOS
            + BRANCH_SCENARIOS
            + HIT_TEST_SCENARIOS
            + FORM_SCENARIOS
            + FLYWHEEL_SCENARIOS
            + NEW_TAB_SCENARIOS
            + KEYS_SCENARIOS
            + CLIENT_HINTS_SCENARIOS
            + JSONP_SCENARIOS
            + FRAMES_SCENARIOS
            + DOCUMENT_SCENARIOS
        )
        if scenario not in known:
            raise KeyError(f"unknown fixture scenario {scenario!r}; known: {known}")
        origin = {
            "service_worker": self.sw_origin,
            "egress": self.ip_origin,
            "localhost": self.localhost_origin,
            "flywheel_drift": self.site_origin(self.DRIFT_HOST),
            "flywheel_blocked": self.site_origin(self.BLOCKED_HOST),
        }.get(scenario, self.main_origin)
        return origin + entry

    def site_origin(self, host: str) -> str:
        """``http://<host>`` when mapped; the main server's loopback origin otherwise."""
        return f"http://{host}" if self.mapped else f"http://127.0.0.1:{self.main_port}"

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
            # its entry page submitted (no request happens without the input event)
            "form_submit": ["/form_submit/get.html", "/form_submit/result.html"],
            "flywheel_shop": [
                "/flywheel_shop/start.html",
                "/flywheel_shop/site.css",
                "/flywheel_shop/logo.png",
            ],
            "flywheel_drift": ["/flywheel_drift/start.html", "/flywheel_drift/site.css"],
            "flywheel_blocked": ["/flywheel_blocked/start.html"],
            # its entry page (no tab opens without an input event)
            "new_tab": ["/new_tab/page.html"],
            # its entry page (nothing is submitted without a key)
            "keys": ["/keys/page.html"],
            "client_hints": ["/client_hints/page.html", "/client_hints/report"],
            "jsonp": ["/jsonp/page.html", "/jsonp/hot.js", "/jsonp/ad.js"],
            "frames": ["/frames/page.html"],
            "slow_document": ["/slow_document/page.html"],
        }
        if scenario not in paths:
            raise KeyError(f"unknown fixture scenario {scenario!r}; known: {sorted(paths)}")
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
        local_main = self.localhost_origin
        local_cross = f"http://localhost:{self.cross_port}"
        local_ws = f"ws://localhost:{self.ws_port}"
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
            # one request of each miss class, to hosts a hermetic recording cannot reach,
            # and a body over the 64 MiB limit
            "/strict/page.html": static(
                self._page(
                    "strict",
                    "<p>strict</p>"
                    '<iframe src="http://frames.example.test/sub.html" '
                    'style="width:200px;height:60px;border:0"></iframe><script>'
                    "navigator.sendBeacon('http://stats.example.test/collect', 'e=1');"
                    "fetch('http://api.example.test/data.json').catch(() => {});"
                    "fetch('/strict/huge.bin').then(r => r.arrayBuffer()).catch(() => {});"
                    "</script>",
                ),
                _HTML,
            ),
            "/strict/huge.bin": self._huge,
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
            # the same routes on exact localhost: served there, its script
            # (paused at the Request stage), a preconnect to cross and a
            # WebSocket (neither ever paused) — all by localhost
            "/localhost/page.html": static(
                self._page(
                    "localhost",
                    f'<link rel="preconnect" href="{local_cross}">'
                    '<button id="local" type="button">pending</button>'
                    f'<script src="{local_main}/localhost/local.js"></script>'
                    f"<script>new WebSocket('{local_ws}/localhost/echo');</script>",
                ),
                _HTML,
            ),
            "/localhost/local.js": static(
                b"document.getElementById('local').textContent = 'served on localhost';\n", _JS
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
                    # submission: M31's flow worked around the stall a submit
                    # button caused (fixed in M33; the ``form_submit`` pages
                    # cover it), and its recorded branches depend on the link.
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
            # ── M32's occlusion page: a sticky header over scrolled links ──
            # Page geometry (CSS px; viewport = page - (0, STICKY_SCROLL_Y)):
            # nav 0..100 (sticky, z-index 10), covered (20,530) 240x40,
            # partial (300,540) 240x100, clear (580,700) 240x40, agree's label
            # (20,800) 240x30, below (20,1800), iframe (860,600) 300x120.
            "/sticky/page.html": static(
                self._page(
                    "sticky header",
                    "<style>body{margin:0;height:2400px;font:16px/20px sans-serif}"
                    "#nav{position:sticky;top:0;height:100px;background:#223;z-index:10}"
                    "#nav a{color:#fff}"
                    ".spot{position:absolute;width:240px;display:block}</style>"
                    '<header id="nav"><a id="home" href="#home">Home</a></header>'
                    '<a id="covered" class="spot" href="#covered" '
                    'style="left:20px;top:530px;height:40px">Covered link</a>'
                    '<a id="partial" class="spot" href="#partial" '
                    'style="left:300px;top:540px;height:100px">Partial link</a>'
                    '<a id="clear" class="spot" href="#clear" '
                    'style="left:580px;top:700px;height:40px">Clear link</a>'
                    '<label id="agree-label" for="agree" class="spot" '
                    'style="left:20px;top:800px;height:30px">'
                    '<input id="agree" type="checkbox" '
                    'style="position:absolute;left:0;top:0;margin:0;width:20px;height:20px">'
                    '<span style="position:absolute;left:0;top:0;width:240px;height:30px;'
                    'background:#eef">I agree</span></label>'
                    '<a id="below" class="spot" href="#below" '
                    'style="left:20px;top:1800px;height:40px">Below the fold</a>'
                    '<iframe id="frame" class="spot" '
                    'style="left:860px;top:600px;width:300px;height:120px;border:0" '
                    'srcdoc=\'<button id="inside" type="button">Inside</button>\'></iframe>'
                    f"<script>window.scrollTo(0, {STICKY_SCROLL_Y});</script>",
                ),
                _HTML,
            ),
            # ── M33's form pages: navigations an input event starts ──
            "/form_submit/get.html": static(self._form_page("GET"), _HTML),
            "/form_submit/post.html": static(self._form_page("POST"), _HTML),
            "/form_submit/enter.html": static(
                self._page(
                    "search",
                    '<form method="GET" action="/form_submit/result.html">'
                    '<p><label for="query">Search</label> '
                    '<input id="query" name="query" type="text" size="20"></p></form>',
                ),
                _HTML,
            ),
            "/form_submit/result.html": self._form_result,
            # ── M33's metrics rehearsal: three sites ──
            **self._flywheel_routes(static),
            # ── tabs a page opens ──
            **self._new_tab_routes(static),
            # ── keys af sends ──
            "/keys/page.html": static(self._keys_page(), _HTML),
            "/keys/result.html": self._keys_result,
            # ── the browser's client hints ──
            "/client_hints/page.html": static(self._client_hints_page(), _HTML),
            "/client_hints/report": static(b"{}", _JSON),
            # ── JSONP callback names ──
            "/jsonp/page.html": static(self._jsonp_page(), _HTML),
            "/jsonp/hot.js": self._jsonp_hot,
            "/jsonp/ad.js": self._jsonp_ad,
            # ── frames the agent never sees ──
            "/frames/page.html": static(self._frames_page(), _HTML),
            # ── documents kept and answered as recorded ──
            "/slow_document/page.html": self._slow_document,
        }

    def _frames_page(self) -> bytes:
        left, top, width, height = FRAMES_PAINT_BOX
        red, green, blue = FRAMES_PAINTED
        return self._page(
            "frames",
            f'<button id="paint" type="button" style="position:absolute;left:{left}px;'
            f'top:{top}px;width:{width}px;height:{height}px">Paint the page</button>'
            "<script>"
            "document.getElementById('paint').onclick = () => {"
            f" document.body.style.background = 'rgb({red}, {green}, {blue})';"
            " document.getElementById('paint').textContent = 'Painted'; };"
            "</script>",
        )

    @staticmethod
    def slow_document_bytes() -> bytes:
        """``slow_document/page.html`` as the server encodes it: GBK, before gzip."""

        def box(name: str) -> str:
            left, top, width, height = SLOW_DOCUMENT_BOXES[name]
            return f"position:absolute;left:{left}px;top:{top}px;width:{width}px;height:{height}px"

        return (
            '<!doctype html><html><head><meta charset="gbk"><link rel="icon" href="data:,">'
            f'<title>slow results</title></head><body><p id="text">{SLOW_DOCUMENT_TEXT}</p>'
            '<a id="again" href="/slow_document/page.html?tab=new" target="_blank" '
            f'style="{box("again")}">again</a>'
            "</body></html>"
        ).encode("gbk")

    def _slow_document(self, handler: _Handler, _body: bytes) -> None:
        """Headers now, the gzip body in two chunks :data:`SLOW_DOCUMENT_DELAY_S` apart."""
        payload = gzip.compress(self.slow_document_bytes(), mtime=0)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=gbk")
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        half = len(payload) // 2
        for part, pause in ((payload[:half], SLOW_DOCUMENT_DELAY_S), (payload[half:], 0.0)):
            handler.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            handler.wfile.flush()
            time.sleep(pause)
        handler.wfile.write(b"0\r\n\r\n")

    def _keys_page(self) -> bytes:
        def box(name: str) -> str:
            left, top, width, height = KEYS_BOXES[name]
            return (
                f"position:absolute;left:{left}px;top:{top}px;width:{width}px;"
                f"height:{height}px;box-sizing:border-box"
            )

        return self._page(
            "keys",
            '<form method="GET" action="/keys/result.html">'
            f'<input id="query" name="query" type="text" style="{box("query")}"></form>'
            f'<input id="react" type="text" style="{box("react")}">'
            f'<textarea id="notes" style="{box("notes")}"></textarea>'
            "<script>"
            "window.__keys = [];"
            "for (const type of ['keydown', 'keypress', 'keyup']) {"
            " addEventListener(type, e => window.__keys.push({type, key: e.key, code: e.code,"
            " keyCode: e.keyCode, which: e.which, charCode: e.charCode,"
            " target: e.target.id || e.target.tagName}), true); }"
            "document.getElementById('react').addEventListener('keydown', e => {"
            " if (e.key === 'Enter' && e.keyCode === 13) { e.preventDefault();"
            " window.__reactEnter = {key: e.key, keyCode: e.keyCode, code: e.code,"
            " which: e.which, value: e.target.value}; } });"
            "</script>",
        )

    def _keys_result(self, handler: _Handler, _body: bytes) -> None:
        query = dict(parse_qsl(handler.path.partition("?")[2], keep_blank_values=True))
        page = self._page(
            "keys result",
            f'<p>Searched for <span id="query">{escape(query.get("query", ""))}</span></p>',
        )
        self._send(handler, 200, page, _HTML)

    def _client_hints_page(self) -> bytes:
        return self._page(
            "client hints",
            '<p id="hints">reading</p><script>'
            "const d = navigator.userAgentData;"
            "const seen = d ? {brands: d.brands.map(b => b.brand + '/' + b.version),"
            " mobile: d.mobile, platform: d.platform} : null;"
            "document.getElementById('hints').textContent = seen"
            " ? 'brands: ' + seen.brands.join(', ') + '; platform: ' + seen.platform"
            " + '; mobile: ' + seen.mobile : 'no userAgentData';"
            "fetch('/client_hints/report?d=' + encodeURIComponent(JSON.stringify(seen)));"
            "</script>",
        )

    def _jsonp_page(self) -> bytes:
        boxes = "".join(f'<div id="{box}"></div>' for box in JSONP_ITEMS)
        return self._page(
            "jsonp",
            f"<p>Hot searches and an ad, by JSONP</p>{boxes}<script>"
            "const t = Math.round(performance.timeOrigin * 1000);"
            "const names = {hot: 'jQuery' + t + '_1', ad: 'ad_' + t.toString(16)};"
            "function show(box, items) {"
            " for (const item of items) { const b = document.createElement('button');"
            " b.id = box + '-' + item; b.type = 'button'; b.textContent = item;"
            " document.getElementById(box).appendChild(b); } }"
            "window[names.hot] = data => show('hot', data.hot);"
            "window[names.ad] = data => show('ad', [data.ad]);"
            "function load(src) { const s = document.createElement('script'); s.src = src;"
            " document.head.appendChild(s); }"
            "load('/jsonp/hot.js?size=3&callback=' + names.hot + '&_=' + t);"
            "load('/jsonp/ad.js?unit=top&cb=' + names.ad);"
            "</script>",
        )

    def _jsonp_answer(self, handler: _Handler, parameter: str) -> str | None:
        """The callback name *handler*'s request asks for in *parameter*, or ``None`` (answered 400)."""
        query = dict(parse_qsl(handler.path.partition("?")[2], keep_blank_values=True))
        name = query.get(parameter, "")
        if not _JSONP_NAME.fullmatch(name):
            self._send(handler, 400, b"bad callback", "text/plain")
            return None
        return name

    def _jsonp_hot(self, handler: _Handler, _body: bytes) -> None:
        name = self._jsonp_answer(handler, "callback")
        if name is not None:
            data = json.dumps({"hot": list(JSONP_ITEMS["hot"])})
            self._send(handler, 200, f"{name}({data});".encode(), "application/json; charset=utf-8")

    def _jsonp_ad(self, handler: _Handler, _body: bytes) -> None:
        name = self._jsonp_answer(handler, "cb")
        if name is not None:
            data = json.dumps({"ad": JSONP_ITEMS["ad"][0]})
            body = f"/**/ typeof {name} === 'function' && {name}({data});"
            self._send(handler, 200, body.encode(), _HTML)

    def _new_tab_routes(
        self, static: Callable[[bytes, str], Callable[[_Handler, bytes], None]]
    ) -> dict[str, Callable[[_Handler, bytes], None]]:
        cross = self.cross_origin
        target = "/new_tab/target.html"

        def control(name: str, kind: str, action: str) -> str:
            left, top, width, height = NEW_TAB_BOXES[name]
            style = (
                f"position:absolute;left:{left}px;top:{top}px;width:{width}px;"
                f"height:{height}px;display:block;margin:0;background:#eef"
            )
            if kind == "link":
                return f'<a id="{name}" style="{style}" {action}>{name}</a>'
            return f'<button id="{name}" type="button" style="{style}" onclick="{action}">{name}</button>'

        def url(how: str, **query: Any) -> str:
            return f"{target}?how={how}&n=1" + "".join(f"&{k}={v}" for k, v in query.items())

        controls = "".join(
            (
                control("blank", "link", f'href="{url("blank", next=1, sub=1)}" target="_blank"'),
                control(
                    "blank_noopener",
                    "link",
                    f'href="{url("blank_noopener", next=1, sub=1)}" target="_blank" rel="noopener"',
                ),
                control(
                    "blank_opener",
                    "link",
                    f'href="{url("blank_opener", next=1, sub=1)}" target="_blank" rel="opener"',
                ),
                control(
                    "blank_cross_site",
                    "link",
                    f'href="{cross}{url("blank_cross_site", next=1, sub=1)}" target="_blank"',
                ),
                control(
                    "window_open", "button", f"window.open('{url('window_open', next=1, sub=1)}')"
                ),
                control(
                    "window_open_noopener",
                    "button",
                    f"window.open('{url('window_open_noopener', next=1, sub=1)}', '_blank', 'noopener')",
                ),
                control(
                    "window_open_cross_site",
                    "button",
                    f"window.open('{cross}{url('window_open_cross_site', next=1, sub=1)}')",
                ),
                control("slow", "link", f'href="{url("slow", slow=2)}" target="_blank"'),
                control("replay", "link", f'href="{url("replay")}" target="_blank"'),
                control(
                    "closes",
                    "link",
                    f'href="{url("closes", close=NEW_TAB_CLOSES_AFTER_MS)}" target="_blank"',
                ),
            )
        )
        opener = self._page(
            "new tabs", f"<script>window.__probe = {NEW_TAB_PROBE};</script>{controls}"
        )

        def report_js(kind: str) -> str:
            return (
                "fetch('/new_tab/report?' + new URLSearchParams({how: q.get('how') || '',"
                f" n: q.get('n') || '', kind: '{kind}', d: JSON.stringify(probe)}}),"
                " {keepalive: true}).catch(() => {});"
            )

        def document(handler: _Handler, _body: bytes) -> None:
            query = dict(parse_qsl(handler.path.partition("?")[2]))
            delay = float(query.get("slow") or 0)
            if delay:
                time.sleep(delay)
            how, n = query.get("how", "?"), int(query.get("n") or 1)
            other = self._other_origin(handler)
            extra = ""
            if query.get("sub") == "1":
                extra = (
                    f"<script>new Worker('/new_tab/worker.js?how={how}&n={n}');</script>"
                    f'<iframe src="{other}/new_tab/frame.html?how={how}&n={n}" '
                    'style="width:300px;height:60px;border:0"></iframe>'
                )
            if query.get("next") == "1" and n == 1:
                extra += (
                    "<script>setTimeout(() => { location.href = "
                    f"'{target}?how={how}&n=2&sub={query.get('sub') or 0}'; }}, 300);</script>"
                )
            if query.get("close"):
                extra += (
                    "<script>addEventListener('load', () => setTimeout(() => window.close(), "
                    f"{int(query['close'])}));</script>"
                )
            page = self._page(
                f"{how} {n}",
                "<script>const q = new URLSearchParams(location.search);"
                f" const probe = {NEW_TAB_PROBE}; {report_js('document')}</script>"
                f'<p>{escape(how)} {n}</p><script src="/new_tab/cacheable.js"></script>{extra}',
            )
            self._send(handler, 200, page, _HTML)

        def cacheable(handler: _Handler, _body: bytes) -> None:
            content = b"window.cacheableLoaded = (window.cacheableLoaded || 0) + 1;\n"
            handler.send_response(200)
            handler.send_header("Content-Type", _JS)
            handler.send_header("Content-Length", str(len(content)))
            handler.send_header("Cache-Control", "public, max-age=3600")
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(content)

        worker = (
            "const q = new URLSearchParams(location.search);"
            f" const probe = {NEW_TAB_PROBE}; {report_js('worker')}"
        ).encode()
        frame = self._page(
            "frame",
            "<script>const q = new URLSearchParams(location.search);"
            f" const probe = {NEW_TAB_PROBE}; {report_js('iframe')}</script><p>frame</p>",
        )
        reporter = (
            "<script>const tag = new URLSearchParams(location.search).get('tag') || '?';"
            " function report() { const o = Intl.DateTimeFormat().resolvedOptions();"
            " fetch('/new_tab/owner_report?' + new URLSearchParams({tag, tz: o.timeZone || '',"
            " locale: o.locale || '', language: navigator.language})).catch(() => {}); }"
            " report(); setInterval(report, 250);</script>"
        )
        owners = self._page(
            "owners",
            f'<iframe id="frame_a" src="{cross}/new_tab/owner.html?tag=frame_a" '
            'style="width:200px;height:50px;border:0"></iframe>'
            "<script>setTimeout(() => { const f = document.createElement('iframe');"
            f" f.id = 'frame_b'; f.src = '{cross}/new_tab/owner.html?tag=frame_b';"
            " f.style.cssText = 'width:200px;height:50px;border:0';"
            " document.body.appendChild(f); }, 1000);</script>"
            '<button id="popup" type="button" style="position:absolute;left:20px;top:200px;'
            'width:300px;height:40px" onclick="window.open(\'/new_tab/owner.html?tag=popup\')">'
            "popup</button>",
        )

        def json_ok(handler: _Handler, _body: bytes) -> None:
            self._send(handler, 200, b'{"ok": true}', _JSON)

        timer = self._page(
            "timer",
            "<p>timer</p><script>addEventListener('load', () => setTimeout(() => "
            f"window.open('{url('timer')}'), {NEW_TAB_TIMER_MS}));</script>",
        )

        return {
            "/new_tab/page.html": static(opener, _HTML),
            "/new_tab/timer.html": static(timer, _HTML),
            target: document,
            "/new_tab/cacheable.js": cacheable,
            "/new_tab/worker.js": static(worker, _JS),
            "/new_tab/frame.html": static(frame, _HTML),
            "/new_tab/report": json_ok,
            "/new_tab/owners.html": static(owners, _HTML),
            "/new_tab/owner.html": static(self._page("owner", reporter), _HTML),
            "/new_tab/owner_report": json_ok,
        }

    def _other_origin(self, handler: _Handler) -> str:
        """The site the request did not go to: main for a request to cross, cross otherwise."""
        host = handler.headers.get("Host", "")
        cross_host = self.cross_origin.split("://", 1)[1]
        return self.main_origin if host == cross_host else self.cross_origin

    def _flywheel_routes(
        self, static: Callable[[bytes, str], Callable[[_Handler, bytes], None]]
    ) -> dict[str, Callable[[_Handler, bytes], None]]:
        css = _flywheel_css()
        head = '<link rel="stylesheet" href="/flywheel_shop/site.css">'
        logo = (
            '<img id="logo" class="box" src="/flywheel_shop/logo.png" style="left:1180px;top:20px">'
        )
        recommend = (
            "<script>fetch('/flywheel_shop/recommend.json?session=' + "
            "Math.floor(performance.timeOrigin)).catch(() => {});</script>"
        )
        password = " + '-' + ".join(f"'{part}'" for part in FLYWHEEL_PASSWORD_PARTS)
        token = " + '_' + ".join(f"'{part}'" for part in FLYWHEEL_TOKEN_PARTS)
        login = (
            "<script>"
            f"const pw = {password}; const token = {token};"
            "document.getElementById('password').value = pw;"
            "fetch('/flywheel_shop/api/me', {headers: {'Authorization': 'Bearer ' + token}})"
            ".catch(() => {});"
            "fetch('/flywheel_shop/api/login', {method: 'POST', headers: "
            "{'Content-Type': 'application/x-www-form-urlencoded'}, "
            "body: 'user=ann&password=' + encodeURIComponent(pw)}).catch(() => {});"
            "</script>"
        )

        def ok_json(handler: _Handler, _body: bytes) -> None:
            self._send(handler, 200, b'{"ok": true}', _JSON)

        def search(handler: _Handler, _body: bytes) -> None:
            query = dict(parse_qsl(handler.path.partition("?")[2], keep_blank_values=True))
            term = escape(query.get("q", ""))
            page = self._page(
                "results",
                head
                + logo
                + f'<h1 class="box" style="left:40px;top:20px">Results for {term}</h1>'
                + _box_link("product", "/flywheel_shop/product.html?id=7", "Blue kettle")
                + '<form method="POST" action="/flywheel_shop/cart">'
                '<input type="hidden" name="item" value="7">'
                + _box_button("quick_add", "Quick add", submit=True)
                + "</form>",
            )
            self._send(handler, 200, page, _HTML)

        def cart(handler: _Handler, body: bytes) -> None:
            if handler.command != "POST":
                self._send(handler, 405, b"POST only", "text/plain")
                return
            fields = dict(parse_qsl(body.decode(), keep_blank_values=True))
            page = self._page(
                "cart",
                head + logo + f'<h1 class="box" style="left:40px;top:20px">'
                f"Added item {escape(fields.get('item', '?'))}</h1>",
            )
            self._send(handler, 200, page, _HTML)

        def blocked(handler: _Handler, _body: bytes) -> None:
            page = self._page(
                "Just a moment...", "<h1>Checking your browser before accessing.</h1>"
            )
            handler.send_response_only(403)
            for name, value in (*FLYWHEEL_CHALLENGE_HEADERS, ("Content-Type", _HTML)):
                handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(page)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(page)

        return {
            "/flywheel_shop/site.css": static(css, "text/css"),
            "/flywheel_shop/logo.png": static(_flywheel_logo(), "image/png"),
            "/flywheel_shop/start.html": static(
                self._page(
                    "kettle shop",
                    head + logo + '<h1 class="box" style="left:40px;top:20px">Kettle shop</h1>'
                    '<form method="GET" action="/flywheel_shop/search.html">'
                    + _box_input("query", "q")
                    + _box_button("search", "Search", submit=True)
                    + "</form>",
                ),
                _HTML,
            ),
            "/flywheel_shop/search.html": search,
            "/flywheel_shop/product.html": static(
                self._page(
                    "blue kettle",
                    head + logo + '<h1 class="box" style="left:40px;top:20px">Blue kettle</h1>'
                    '<p class="box" style="left:40px;top:100px">30 EUR</p>' + recommend,
                ),
                _HTML,
            ),
            "/flywheel_shop/recommend.json": ok_json,
            "/flywheel_shop/cart": cart,
            "/flywheel_shop/login.html": static(
                self._page(
                    "sign in",
                    head + logo + '<h1 class="box" style="left:40px;top:20px">Sign in</h1>'
                    '<input id="user" class="box" value="ann" style="left:40px;top:100px;'
                    'width:300px;height:32px">'
                    '<input id="password" type="password" class="box" style="left:40px;'
                    'top:160px;width:300px;height:32px">' + login,
                ),
                _HTML,
            ),
            "/flywheel_shop/api/me": ok_json,
            "/flywheel_shop/api/login": ok_json,
            "/flywheel_drift/site.css": static(css, "text/css"),
            "/flywheel_drift/start.html": static(
                self._page(
                    "news",
                    '<link rel="stylesheet" href="/flywheel_drift/site.css">'
                    '<h1 class="box" style="left:40px;top:20px">News</h1>'
                    + _box_button("drift", "Read today's story")
                    + "<script>document.getElementById('drift').onclick = () => {"
                    "location.href = '/flywheel_drift/next.html?visit=' + "
                    "Math.floor(performance.timeOrigin); };</script>",
                ),
                _HTML,
            ),
            "/flywheel_drift/next.html": static(
                self._page(
                    "story",
                    '<link rel="stylesheet" href="/flywheel_drift/site.css">'
                    '<h1 class="box" style="left:40px;top:20px">Today\'s story</h1>',
                ),
                _HTML,
            ),
            "/flywheel_blocked/start.html": blocked,
        }

    def _form_page(self, method: str) -> bytes:
        return self._page(
            f"{method.lower()} order form",
            f'<form method="{method}" action="/form_submit/result.html">'
            '<p><label for="item">Item</label> '
            '<input id="item" name="item" type="text" size="20" value="kettle"></p>'
            '<p><button id="add" type="submit">Add to cart</button></p></form>',
        )

    def _form_result(self, handler: _Handler, body: bytes) -> None:
        """Echo the submitted fields — from the query (GET) or the urlencoded body (POST)."""
        query = handler.path.partition("?")[2] if handler.command == "GET" else body.decode()
        fields = parse_qsl(query, keep_blank_values=True)
        received = ", ".join(f"{name}={value}" for name, value in fields) or "(nothing)"
        page = self._page(
            "form result",
            f'<p>Received by {handler.command}: <span id="received">{escape(received)}</span></p>',
        )
        self._send(handler, 200, page, _HTML)

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

    @staticmethod
    def _huge(handler: _Handler, _body: bytes) -> None:
        """:data:`HUGE_BODY_BYTES`, written block by block."""
        block = bytes(range(256)) * 4096  # 1 MiB
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(HUGE_BODY_BYTES))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if handler.command == "HEAD":
            return
        for _ in range(HUGE_BODY_BYTES // len(block)):
            handler.wfile.write(block)

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


# ── the flywheel pages' pieces ────────────────────────────────────────


def _flywheel_css() -> bytes:
    """The sites' stylesheet: every control absolutely placed, no caret (deterministic pixels)."""
    return (
        b"body{margin:0;font:16px/20px sans-serif;background:#fff;color:#111}\n"
        b".box{position:absolute;box-sizing:border-box;margin:0}\n"
        b"input{caret-color:transparent;font:16px/20px sans-serif}\n"
        b"button{font:16px/20px sans-serif}\n"
    )


def _flywheel_logo() -> bytes:
    """A 48x48 PNG of deterministic noise: a body worth deduplicating, the same every run."""
    pixels = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(216))
    buffer = io.BytesIO()
    Image.frombytes("RGB", (48, 48), pixels[: 48 * 48 * 3]).save(buffer, format="PNG")
    return buffer.getvalue()


def _style(name: str) -> str:
    left, top, width, height = FLYWHEEL_BOXES[name]
    return f"left:{left}px;top:{top}px;width:{width}px;height:{height}px"


def _box_input(name: str, field: str) -> str:
    return f'<input id="{name}" name="{field}" type="text" class="box" style="{_style(name)}">'


def _box_button(name: str, label: str, *, submit: bool = False) -> str:
    kind = "submit" if submit else "button"
    return f'<button id="{name}" type="{kind}" class="box" style="{_style(name)}">{label}</button>'


def _box_link(name: str, href: str, label: str) -> str:
    return f'<a id="{name}" href="{href}" class="box" style="{_style(name)}">{label}</a>'


def flywheel_point(name: str) -> tuple[int, int]:
    """The centre of a flywheel control (:data:`FLYWHEEL_BOXES`) — where UI-TARS clicks it."""
    left, top, width, height = FLYWHEEL_BOXES[name]
    return left + width // 2, top + height // 2


def keys_point(name: str) -> tuple[int, int]:
    """The centre of a ``keys`` field (:data:`KEYS_BOXES`)."""
    left, top, width, height = KEYS_BOXES[name]
    return left + width // 2, top + height // 2


def new_tab_point(name: str) -> tuple[int, int]:
    """The centre of a ``new_tab`` control (:data:`NEW_TAB_BOXES`)."""
    left, top, width, height = NEW_TAB_BOXES[name]
    return left + width // 2, top + height // 2
