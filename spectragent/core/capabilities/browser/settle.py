"""Waiting for the page to settle before an observation captures it (``docs/architecture_design.md`` §4.14).

The 2026-10-13 live smoke run compared recorded and replayed screenshots and
found the browser observation captured pages before they had rendered: a
blank white page right after a click (nvidia), a hero image without the
booking form the page renders a moment later (megabus). Recording runs on the
live network, which is slow; a replay is served from the archive and is
fast, so the same step's replay captured the finished page and the
restoration's screenshot check refused it. And the agent decided on the
blank page, in every browser run.

So every observation waits for the page to settle, in recording and in
replay alike, before anything of it is captured. The wait is part of the
observation itself: it runs once per capture attempt, ahead of the one
``BrowserStateRequestEvent`` that captures every negotiated fragment, so a
DOM-only observation waits exactly as a screenshot one does, and it judges
the page by its state, never by taking or comparing images.

**Settled** means three things, judged in this order:

- **load** — the main frame's current document reached Chrome's ``load``
  lifecycle event (:class:`~spectragent.core.capabilities.browser.navigation.MainFrameLoad`):
  everything the document itself asked for has arrived;
- **dom** — then nothing changed for the quiet window (``settle_quiet_ms``):
  no DOM mutation and no resource finishing loading, measured in the page
  *from the moment the wait reaches it* — what happened before does not
  count, because an action's effects (a click's request, the render after
  it) start after the action returned. A ``MutationObserver`` and a
  ``PerformanceObserver`` in an isolated world (:data:`SETTLE_WORLD`) watch
  for it, so page scripts neither see nor disturb the measurement. A
  resource that finishes is the network activity worth waiting for: an image
  or a web font changes the page without a DOM mutation, a request's answer
  is usually rendered right after it. A request still in flight with nothing
  else happening for the window (a poll, a stream, a beacon) is not waited for;
- **content** — and the viewport shows something: visible text, or a visible
  graphic element (an image, a video, a canvas, an SVG drawing, a frame, a
  form control) at least :data:`_CONTENT_MIN_GRAPHIC_PX` in each direction.
  A quiet page with an empty viewport is still waiting for what it will
  show, and the wait goes on, within the same cap: the 2026-10-22 lookahead
  data had two captures recorded as settled that were a single-page app's
  white shell, a 3 px loading bar along the top, quiet for seconds while its
  script was still downloading. Colour painted by CSS (a loading bar, a
  skeleton, the page's background) is not content; ``about:blank`` has
  nothing coming and is not waited on. The same probe judges it
  (:data:`_SETTLE_PROBE`): when the window is quiet, and again every
  :data:`_CONTENT_RECHECK_MS` while the viewport stays empty, it walks the
  DOM in document order up to the first visible content, at most
  :data:`_CONTENT_MAX_NODES` nodes.

Chrome's ``networkAlmostIdle`` was measured as a further condition and left
out: on 40 real sites it added about a second to the wait after a page load
and made no capture more complete (the design doc's table, §4.14).

The load is per document (Chrome reports it once), and a document is waited
for it at most one cap in total (:meth:`MainFrameLoad.wait_loaded`): a page
whose load never finishes costs the cap once, not on every step. The quiet
window and the content are judged on every observation.

Everything ends at the cap (``settle_max_wait_s``); the capture then goes
ahead anyway — a page that never goes quiet (a carousel, a video, a ticker)
or whose viewport stays empty is captured as it is, never waited on forever.
A page whose state cannot be read (no main frame id, a document that refuses
an isolated world) is captured at once; a renderer too busy to answer the
probe is captured at the cap. Each capture records how its wait went
(:class:`PageSettle`, on every browser fragment of that capture).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from agent_cdp import CDPCommandProtocol

    from spectragent.core.capabilities.browser.navigation import (
        MainFrameLoad,
        MainFrameNavigation,
    )

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: The isolated world the wait is measured in (``Page.createIsolatedWorld``):
#: one per document, reused by name, invisible to the page's own scripts.
SETTLE_WORLD = "af-settle"

#: The default quiet window, in milliseconds (``BrowserCapability.settle_quiet_ms``).
DEFAULT_SETTLE_QUIET_MS = 500
#: The default cap on the whole wait, in seconds (``BrowserCapability.settle_max_wait_s``).
DEFAULT_SETTLE_MAX_WAIT_S = 5.0

#: How long past the wait's own end a reply from the page may take to arrive: the
#: probe resolves at its budget, and only a renderer too busy to run it is later.
_REPLY_GRACE_S = 0.25
#: How many documents one wait follows: a document replaced during the wait
#: (a client-side redirect) is waited for in its place, within the same cap.
_MAX_DOCUMENTS = 3

#: How often a quiet page whose viewport is still empty is looked at again, in
#: milliseconds. Content can appear with no DOM change and no resource finishing
#: (a CSS animation or transition revealing what is already in the DOM), so a
#: quiet empty page is not left alone until the cap. A look at an empty page
#: walks a small DOM (measured on local pages: under 0.1 ms; the 10jqka shell,
#: 32 nodes, 0.2 ms; never more than about 10 ms, :data:`_CONTENT_MAX_NODES`),
#: so ten looks a second cost the page next to nothing and such content is
#: seen within a tenth of a second.
_CONTENT_RECHECK_MS = 100
#: The smallest graphic element that counts as content: the part of it inside
#: the viewport, in CSS px, in each direction. Below it in either direction are
#: hairline rules, 1×1 tracking pixels and loading bars drawn as an image or an
#: SVG (2–5 px tall; the bar on the blank 10jqka captures is 3 px). 16 px is the
#: smallest icon size pages commonly draw; a page that shows nothing bigger
#: than a smaller icon is still an empty shell.
_CONTENT_MIN_GRAPHIC_PX = 16
#: The smallest box of text that counts as visible text: the part of it inside
#: the viewport and inside a parent that clips its overflow, in CSS px, in each
#: direction. Smaller is text squeezed out of sight: ``font-size: 0`` (a zero
#: box), or screen-reader-only text in its 1×1 clipping box (Bootstrap's
#: ``visually-hidden`` "Loading…" inside a CSS spinner, a page with nothing to
#: show yet). Legible text is several times taller, so 4 px keeps a line the
#: viewport's edge cuts through.
_CONTENT_MIN_TEXT_PX = 4
#: How many DOM nodes one look walks at most before it counts the viewport as
#: showing content. The walk stops at the first visible content: on ten real
#: sites (2026-10-22), at the top and scrolled to the middle and the end, it
#: stopped within 8–2,992 nodes in at most 4.5 ms (the longest: douban scrolled
#: to its end). Only a page with this many rendered nodes before anything in
#: view gets here — one scrolled far past its content, not an empty shell —
#: and walking this many costs about 10 ms (measured: 20,000 paragraphs
#: scrolled past), which bounds a look on any DOM.
_CONTENT_MAX_NODES = 5000
#: The elements whose own box is something drawn: an image, a video, a canvas,
#: an SVG drawing, a frame, a form control (a field or a button draws its own
#: box, its label aside). What is inside one is part of it.
_CONTENT_GRAPHICS: tuple[str, ...] = (
    "img",
    "video",
    "canvas",
    "svg",
    "iframe",
    "frame",
    "embed",
    "object",
    "input",
    "textarea",
    "select",
    "button",
)

#: The browser fragments a recorded observation keeps its settle record on, cheapest to
#: read first: every fragment of one capture carries the same record (``BrowserCaptureFragment``).
SETTLE_RECORD_FRAGMENTS: tuple[str, ...] = (
    "browser.browser_meta",
    "browser.screenshot",
    "browser.dom",
)

#: How a settle wait ended.
SettleOutcome = Literal["settled", "capped", "unknown"]
#: What a capped wait had not seen settle (in the order they are judged).
SettlePending = Literal["load", "dom", "content"]
#: Every settle condition, in the order they are judged: what a wait that never
#: got to probe the page (its load did not come in time) had not seen.
SETTLE_CONDITIONS: tuple[SettlePending, ...] = ("load", "dom", "content")

#: Substrings of the errors Chrome answers a probe with when its document went away.
_DOCUMENT_GONE = (
    "Execution context was destroyed",
    "Cannot find context with specified id",
    "Inspected target navigated or closed",
)

#: The in-page half: resolves once nothing changed for the quiet window and the
#: viewport shows content, or at the budget, reporting both. ``%(quiet_ms)d`` /
#: ``%(budget_ms)d`` are filled in per probe, the rest from the constants above.
_SETTLE_PROBE = """
(async (quietMs, budgetMs) => {
  const recheckMs = %(recheck_ms)d;
  const maxNodes = %(max_nodes)d;
  const minGraphicPx = %(min_graphic_px)d;
  const minTextPx = %(min_text_px)d;
  const graphics = new Set(%(graphics)s);
  let width = 0;
  let height = 0;
  const inView = (left, top, right, bottom, min) =>
    Math.min(right, width) - Math.max(left, 0) >= min &&
    Math.min(bottom, height) - Math.max(top, 0) >= min;
  const range = document.createRange();
  const textShown = (text) => {
    if (!/\\S/.test(text.data)) return false;
    range.selectNodeContents(text);
    const rects = Array.from(range.getClientRects()).filter(
      (r) => inView(r.left, r.top, r.right, r.bottom, minTextPx));
    if (rects.length === 0) return false;
    const parent = text.parentElement;
    if (parent === null) return true;
    const style = getComputedStyle(parent);
    if (style.visibility !== "visible") return false;
    if (style.overflowX === "visible" && style.overflowY === "visible") return true;
    const box = parent.getBoundingClientRect();
    return rects.some((r) => inView(
      Math.max(r.left, box.left), Math.max(r.top, box.top),
      Math.min(r.right, box.right), Math.min(r.bottom, box.bottom), minTextPx));
  };
  const graphicShown = (element) => {
    if (element.localName === "img" && !element.complete) return false;
    if (element.localName === "svg" && element.firstElementChild === null) return false;
    if (!element.checkVisibility({visibilityProperty: true})) return false;
    const r = element.getBoundingClientRect();
    return inView(r.left, r.top, r.right, r.bottom, minGraphicPx);
  };
  const showsContent = () => {
    if (location.href === "about:blank") return true;
    const root = document.documentElement;
    if (root === null) return false;
    width = window.innerWidth;
    height = window.innerHeight;
    const next = [root];
    for (let visited = 0; next.length > 0; visited++) {
      if (visited >= maxNodes) return true;
      const node = next.pop();
      if (node.nextSibling !== null) next.push(node.nextSibling);
      if (node.nodeType === 3) {
        if (textShown(node)) return true;
        continue;
      }
      if (node.nodeType !== 1) continue;
      if (
        !node.checkVisibility({opacityProperty: true, contentVisibilityAuto: true}) &&
        getComputedStyle(node).display !== "contents"
      ) continue;
      if (graphics.has(node.localName)) {
        if (graphicShown(node)) return true;
        continue;
      }
      if (node.firstChild !== null) next.push(node.firstChild);
      const shadow = node.shadowRoot;
      if (shadow && shadow.firstChild !== null) next.push(shadow.firstChild);
    }
    return false;
  };
  const started = performance.now();
  const deadline = started + budgetMs;
  let last = started;
  let mutations = 0;
  let resources = 0;
  const changes = new MutationObserver((records) => {
    mutations += records.length;
    last = performance.now();
  });
  changes.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  let loads = null;
  try {
    loads = new PerformanceObserver((list) => {
      resources += list.getEntries().length;
      last = performance.now();
    });
    loads.observe({type: "resource"});
  } catch (error) {
    loads = null;
  }
  let emptyAt = -Infinity;
  try {
    for (;;) {
      const now = performance.now();
      const quiet = now - last >= quietMs;
      const waited_ms = Math.round(now - started);
      if (now >= deadline) {
        return {quiet, content: showsContent(), waited_ms, mutations, resources};
      }
      if (quiet && (last > emptyAt || now - emptyAt >= recheckMs)) {
        if (showsContent()) {
          return {quiet: true, content: true, waited_ms, mutations, resources};
        }
        emptyAt = now;
      }
      const pause = quiet
        ? Math.min(emptyAt + recheckMs - now, deadline - now)
        : Math.min(quietMs - (now - last), deadline - now);
      await new Promise((resolve) => setTimeout(resolve, Math.max(1, pause)));
    }
  } finally {
    changes.disconnect();
    if (loads) loads.disconnect();
  }
})(%(quiet_ms)d, %(budget_ms)d)
"""


class PageSettle(BaseModel):
    """How one capture waited for the page to settle — every browser fragment of it carries this.

    ``waited_ms``: the wall time of the wait, from its start to the capture
    it preceded. ``outcome``: ``settled`` (the document loaded, the page
    stayed quiet for the window and its viewport showed content, before the
    cap), ``capped`` (the cap ended the wait; ``waiting_for`` names what the
    wait had not seen settle, in the order it judges them: a condition the
    page did not meet when the cap came, and every one after a condition the
    wait never got past — ``["content"]`` alone is a quiet page whose
    viewport stayed empty) or ``unknown`` (the page's state could not be
    read — ``detail`` says why — and the capture went ahead at once).

    ``main_document_loaded`` is a fact about the page, not about the wait
    (§4.4, "初次加载是否完成"): whether the main frame's current document had
    reached its ``load`` lifecycle event when the wait ended, immediately
    before the capture — read from the tab's :class:`MainFrameLoad`, the
    tracker the wait itself uses. ``None`` when that is not known: no tracker,
    or no lifecycle event of the main frame heard. It does not follow from
    ``outcome``: a document whose load never finishes is waited on for one
    cap in total, so a later capture of it can be ``settled`` and still say
    ``False``. ``None`` is also what a record without the field reads as.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    waited_ms: int = Field(ge=0)
    outcome: SettleOutcome
    waiting_for: list[SettlePending] = Field(default_factory=list)
    detail: str | None = None
    #: Whether the main document had reached its ``load`` event at capture (class docstring).
    main_document_loaded: bool | None = None

    @property
    def capped(self) -> bool:
        """Whether the cap ended the wait."""
        return self.outcome == "capped"


class _PageWatch(BaseModel):
    """What one in-page probe reported, or why there is no report."""

    #: The page was quiet for the window when the probe ended.
    quiet: bool = False
    #: The viewport showed content when the probe ended.
    content: bool = False
    waited_ms: int = 0
    mutations: int = 0
    resources: int = 0
    #: The probe's document went away mid-probe (a navigation): wait for the next one.
    document_gone: bool = False
    #: The probe could not run or did not answer; the page state is unknown.
    error: str | None = None

    def pending(self) -> list[SettlePending]:
        """The conditions the page did not meet when the probe ended, in judged order."""
        met: dict[SettlePending, bool] = {"dom": self.quiet, "content": self.content}
        return [condition for condition, ok in met.items() if not ok]


async def _bounded(reply: Awaitable[_T], timeout: float) -> _T:
    return await asyncio.wait_for(reply, max(timeout, 0.0))


def _gone(error: object) -> bool:
    text = str(error)
    return any(marker in text for marker in _DOCUMENT_GONE)


def _probe_expression(quiet_ms: int, budget_ms: int) -> str:
    return _SETTLE_PROBE % {
        "quiet_ms": quiet_ms,
        "budget_ms": budget_ms,
        "recheck_ms": _CONTENT_RECHECK_MS,
        "max_nodes": _CONTENT_MAX_NODES,
        "min_graphic_px": _CONTENT_MIN_GRAPHIC_PX,
        "min_text_px": _CONTENT_MIN_TEXT_PX,
        "graphics": json.dumps(list(_CONTENT_GRAPHICS)),
    }


async def watch_page(
    cdp: CDPCommandProtocol,
    *,
    session_id: str | None,
    frame_id: str,
    quiet_ms: int,
    budget_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> _PageWatch:
    """Wait, in the page, for a quiet window of *quiet_ms* over a viewport showing content; at most *budget_s*.

    Two commands on the tab's session: ``Page.createIsolatedWorld`` (the
    document's :data:`SETTLE_WORLD`, created once and then found by name) and
    one ``Runtime.evaluate`` awaiting the probe's promise, which resolves by
    itself at what is left of the budget, saying whether the page was quiet
    and showed content. A reply that is not the probe's (a test double, a
    document that refused the world) is an ``error``; a document that went
    away during the probe is ``document_gone``; a page that does not answer
    by the end of the budget (its main thread busy, a dialog open) is a
    probe that saw neither.
    """
    deadline = clock() + max(budget_s, 0.0)

    def left() -> float:
        return max(deadline - clock(), 0.0)

    try:
        world = await _bounded(
            cdp.send(
                "Page.createIsolatedWorld",
                {"frameId": frame_id, "worldName": SETTLE_WORLD},
                session_id=session_id,
            ),
            left() + _REPLY_GRACE_S,
        )
    except TimeoutError:
        return _PageWatch(waited_ms=int(budget_s * 1000))
    except Exception as exc:
        if _gone(exc):
            return _PageWatch(document_gone=True)
        return _PageWatch(error=f"the settle world could not be created: {exc}")
    context = (world or {}).get("executionContextId") if isinstance(world, dict) else None
    if not isinstance(context, int):
        return _PageWatch(error="the settle world has no execution context")

    remaining = left()
    expression = _probe_expression(quiet_ms, int(remaining * 1000))
    try:
        reply = await _bounded(
            cdp.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "contextId": context,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=session_id,
            ),
            remaining + _REPLY_GRACE_S,
        )
    except TimeoutError:
        # The page's main thread never ran the probe to its end: nothing seen.
        return _PageWatch(waited_ms=int(budget_s * 1000))
    except Exception as exc:
        if _gone(exc):
            return _PageWatch(document_gone=True)
        return _PageWatch(error=f"the settle probe failed: {exc}")
    if not isinstance(reply, dict):
        return _PageWatch(error="the settle probe returned no result")
    details = reply.get("exceptionDetails")
    if details:
        if _gone(json.dumps(details)):
            return _PageWatch(document_gone=True)
        return _PageWatch(error="the settle probe raised in the page")
    value = (reply.get("result") or {}).get("value")
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("quiet"), bool)
        or not isinstance(value.get("content"), bool)
    ):
        return _PageWatch(error="the settle probe returned no result")
    try:
        return _PageWatch.model_validate(value)
    except ValueError:
        return _PageWatch(error="the settle probe returned no result")


async def wait_for_settle(
    cdp: CDPCommandProtocol,
    *,
    session_id: str | None,
    frame_id: str | None,
    navigation: MainFrameNavigation | None,
    load: MainFrameLoad | None,
    quiet_ms: int,
    max_wait_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> PageSettle:
    """Wait for the tab's page to settle (module docstring), at most *max_wait_s*.

    Never raises for the page's sake: whatever cannot be read ends the wait
    as ``unknown`` and the capture goes ahead. Whichever way it ends, the
    record says whether the main document had loaded by then
    (``PageSettle.main_document_loaded``, read from *load*; ``None`` without
    one).
    """
    started = clock()
    deadline = started + max_wait_s

    def settle(
        outcome: SettleOutcome,
        waiting_for: Iterable[SettlePending] = (),
        detail: str | None = None,
    ) -> PageSettle:
        return PageSettle(
            waited_ms=max(0, round((clock() - started) * 1000)),
            outcome=outcome,
            waiting_for=list(waiting_for),
            detail=detail,
            main_document_loaded=load.loaded if load is not None else None,
        )

    for document in range(_MAX_DOCUMENTS):
        # The caller waited out a navigation in flight before the wait began.
        # One the page started during the wait (it replaced the document the
        # probe ran in) is waited out here, within the same cap.
        if (
            document
            and navigation is not None
            and navigation.in_flight
            and not await navigation.wait_settled(deadline - clock())
        ):
            return settle("capped", SETTLE_CONDITIONS)
        if load is not None and not await load.wait_loaded(deadline - clock(), budget_s=max_wait_s):
            return settle("capped", SETTLE_CONDITIONS)
        if frame_id is None:
            return settle("unknown", detail="the tab's main frame is not known")
        remaining = deadline - clock()
        watch = await watch_page(
            cdp,
            session_id=session_id,
            frame_id=frame_id,
            quiet_ms=quiet_ms,
            budget_s=max(remaining, 0.0),
            clock=clock,
        )
        if watch.document_gone:
            if clock() < deadline:
                continue
            return settle("capped", SETTLE_CONDITIONS)
        if watch.error is not None:
            logger.debug("settle (sid=%s): %s; capturing now", session_id, watch.error)
            return settle("unknown", detail=watch.error)
        pending = watch.pending()
        if not pending:
            return settle("settled")
        return settle("capped", pending)
    return settle("capped", SETTLE_CONDITIONS)


def recorded_settle(read: Callable[[str], Any], recorded: Iterable[str]) -> PageSettle | None:
    """The settle record of one recorded observation, or ``None`` when it has none.

    *recorded* names the fragments the observation recorded (fully qualified,
    captures that failed left out); *read* reads one of them back. The first
    of :data:`SETTLE_RECORD_FRAGMENTS` the observation holds is read — they
    all carry the capture's one record. ``None``: no browser fragment, or a
    capture taken with the wait turned off (or recorded before the wait
    existed).
    """
    present = set(recorded)
    for key in SETTLE_RECORD_FRAGMENTS:
        if key in present:
            record = getattr(read(key), "settle", None)
            return record if isinstance(record, PageSettle) else None
    return None


__all__ = [
    "DEFAULT_SETTLE_MAX_WAIT_S",
    "DEFAULT_SETTLE_QUIET_MS",
    "SETTLE_CONDITIONS",
    "SETTLE_RECORD_FRAGMENTS",
    "SETTLE_WORLD",
    "PageSettle",
    "SettleOutcome",
    "SettlePending",
    "recorded_settle",
    "wait_for_settle",
    "watch_page",
]
