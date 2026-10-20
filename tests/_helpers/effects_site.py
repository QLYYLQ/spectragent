"""A small local site for measuring action effects and capture state in a real Chrome.

One threaded HTTP server on ``127.0.0.1`` with an ephemeral port, serving
deterministic pages under ``/effects/`` (no web fonts, no animation unless a
page is about it, ``<link rel=icon href="data:,">`` so no favicon request):

- ``scroll.html`` — a page :data:`SCROLL_PAGE_HEIGHT` px tall, with a scroll
  box ``#inner`` at :data:`BOXES` ``["inner"]`` whose content is
  :data:`INNER_CONTENT_HEIGHT` px tall;
- ``short.html`` — a page that cannot scroll at all;
- ``click.html`` — a button ``#go`` whose click moves focus to the ``Name``
  field, a button ``#under`` (``Hidden target``), text field ``#name``
  (``Name``), password field ``#pw`` (``Password``), a ``<select id=fruit>``
  (Apple / Banana / Cherry) and a ``Cover`` script hook: ``window.cover()``
  puts an element labelled ``Cookie banner`` over ``#under``;
- ``image.html`` — a visible image that fails to load (``missing.png``, a 404),
  a visible one that loads (``ok.png``) and a failing one far below the fold;
- ``opener.html`` — a ``target=_blank`` link ``#newtab`` to ``second.html``;
- ``busy.html`` — text that changes every 50 ms, so the page never goes quiet;
- ``a.html`` / ``b.html`` — two pages for navigation and history.

Every control sits at a fixed CSS-px box (:data:`BOXES`), so a test clicks by
coordinate without reading the DOM.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from PIL import Image

#: Height of ``scroll.html``'s document, CSS px.
SCROLL_PAGE_HEIGHT = 3000
#: Height of the content inside ``scroll.html``'s ``#inner`` box, CSS px.
INNER_CONTENT_HEIGHT = 1200
#: ``(left, top, width, height)`` in CSS px of every control a test aims at.
BOXES: dict[str, tuple[int, int, int, int]] = {
    "inner": (40, 120, 300, 200),
    "go": (20, 20, 200, 40),
    "under": (20, 80, 200, 40),
    "name": (20, 140, 240, 32),
    "pw": (20, 190, 240, 32),
    "fruit": (20, 240, 200, 32),
    "broken": (20, 20, 200, 150),
    "ok": (240, 20, 200, 150),
    "newtab": (20, 20, 200, 40),
    "link_b": (20, 20, 200, 40),
}

_HEAD = '<meta charset="utf-8"><link rel="icon" href="data:,">'


def _box(name: str) -> str:
    left, top, width, height = BOXES[name]
    return f"position:absolute;left:{left}px;top:{top}px;width:{width}px;height:{height}px;"


def centre(name: str) -> tuple[int, int]:
    """The centre of a control's box, in CSS px of the viewport (page not scrolled)."""
    left, top, width, height = BOXES[name]
    return left + width // 2, top + height // 2


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _page(title: str, body: str, *, style: str = "") -> bytes:
    return (
        f"<!doctype html><html><head>{_HEAD}<title>{title}</title>"
        f"<style>body{{margin:0;font:16px sans-serif}}{style}</style></head>"
        f"<body>{body}</body></html>"
    ).encode()


def _pages() -> dict[str, tuple[str, bytes]]:
    lines = "".join(f"<p>Line {n}</p>" for n in range(60))
    return {
        "scroll.html": (
            "text/html; charset=utf-8",
            _page(
                "Scroll",
                f'<div id="inner" style="{_box("inner")}overflow:auto;border:1px solid #888">'
                f'<div style="height:{INNER_CONTENT_HEIGHT}px">{lines}</div></div>'
                f'<div style="height:{SCROLL_PAGE_HEIGHT}px"></div>',
            ),
        ),
        "short.html": ("text/html; charset=utf-8", _page("Short", "<p>Nothing to scroll.</p>")),
        "click.html": (
            "text/html; charset=utf-8",
            _page(
                "Click",
                f'<button id="go" style="{_box("go")}" '
                "onclick=\"document.getElementById('name').focus()\">Go</button>"
                f'<button id="under" style="{_box("under")}">Hidden target</button>'
                f'<label style="position:absolute;left:280px;top:140px" for="name">Name</label>'
                f'<input id="name" aria-label="Name" style="{_box("name")}">'
                f'<input id="pw" type="password" aria-label="Password" style="{_box("pw")}">'
                f'<select id="fruit" aria-label="Fruit" style="{_box("fruit")}">'
                "<option>Apple</option><option>Banana</option><option>Cherry</option></select>"
                "<script>window.cover = function () {"
                " const c = document.createElement('div'); c.id = 'cover';"
                " c.setAttribute('aria-label', 'Cookie banner');"
                f" c.style.cssText = '{_box('under')}z-index:10;background:#fc0';"
                " document.body.appendChild(c); };</script>",
            ),
        ),
        "image.html": (
            "text/html; charset=utf-8",
            _page(
                "Images",
                f'<img id="broken" src="missing.png" alt="Product photo" style="{_box("broken")}">'
                f'<img id="ok" src="ok.png" alt="Logo" style="{_box("ok")}">'
                '<img id="below" src="missing-below.png" alt="Footer"'
                ' style="position:absolute;left:20px;top:2400px;width:200px;height:150px">'
                '<div style="height:3000px"></div>',
            ),
        ),
        "opener.html": (
            "text/html; charset=utf-8",
            _page(
                "Opener",
                f'<a id="newtab" href="second.html" target="_blank" style="{_box("newtab")}'
                'display:block">Open second</a>',
            ),
        ),
        "second.html": ("text/html; charset=utf-8", _page("Second", "<p>Second page.</p>")),
        "busy.html": (
            "text/html; charset=utf-8",
            _page(
                "Busy",
                '<p id="n">0</p><script>let n = 0;'
                " setInterval(() => { document.getElementById('n').textContent = ++n; }, 50);"
                "</script>",
            ),
        ),
        "a.html": (
            "text/html; charset=utf-8",
            _page(
                "Page A",
                f'<a id="link_b" href="b.html" style="{_box("link_b")}display:block">To B</a>',
            ),
        ),
        "b.html": ("text/html; charset=utf-8", _page("Page B", "<p>Page B.</p>")),
        "ok.png": ("image/png", _png((30, 120, 200))),
    }


class _Handler(BaseHTTPRequestHandler):
    server: Any

    def log_message(self, *_args: Any) -> None:
        return None

    def do_GET(self) -> None:  # noqa: N802 — the stdlib's handler name
        path = self.path.split("?", 1)[0]
        name = path.removeprefix("/effects/")
        page = self.server.pages.get(name) if path.startswith("/effects/") else None
        if page is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        content_type, body = page
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class EffectsSite:
    """The running site; :meth:`url` names a page."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.pages = _pages()  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def url(self, page: str) -> str:
        """The URL of one page under ``/effects/``."""
        return f"{self.origin}/effects/{page}"

    def start(self) -> EffectsSite:
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def effects_site() -> Iterator[EffectsSite]:
    """Start the site for the duration of a ``with`` block."""
    site = EffectsSite().start()
    try:
        yield site
    finally:
        site.close()
