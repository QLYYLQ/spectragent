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

**Settled** means two things, judged in this order:

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
  else happening for the window (a poll, a stream, a beacon) is not waited for.

Chrome's ``networkAlmostIdle`` was measured as a third condition and left
out: on 40 real sites it added about a second to the wait after a page load
and made no capture more complete (the design doc's table, §4.14).

The load is per document (Chrome reports it once), and a document is waited
for it at most one cap in total (:meth:`MainFrameLoad.wait_loaded`): a page
whose load never finishes costs the cap once, not on every step. The quiet
window is measured on every observation.

Everything ends at the cap (``settle_max_wait_s``); the capture then goes
ahead anyway — a page that never goes quiet (a carousel, a video, a ticker)
is captured as it is, never waited on forever. A page whose state cannot be
read (no main frame id, a document that refuses an isolated world) is
captured at once; a renderer too busy to answer the probe is captured at the
cap. Each capture records how its wait went (:class:`PageSettle`, on every
browser fragment of that capture).
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

#: The isolated world the quiet window is measured in (``Page.createIsolatedWorld``):
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

#: The browser fragments a recorded observation keeps its settle record on, cheapest to
#: read first: every fragment of one capture carries the same record (``BrowserCaptureFragment``).
SETTLE_RECORD_FRAGMENTS: tuple[str, ...] = (
    "browser.browser_meta",
    "browser.screenshot",
    "browser.dom",
)

#: How a settle wait ended.
SettleOutcome = Literal["settled", "capped", "unknown"]
#: What a capped wait had not yet seen settle (in the order they are judged).
SettlePending = Literal["load", "dom"]

#: Substrings of the errors Chrome answers a probe with when its document went away.
_DOCUMENT_GONE = (
    "Execution context was destroyed",
    "Cannot find context with specified id",
    "Inspected target navigated or closed",
)

#: The in-page half: resolves once nothing changed for the quiet window, or at
#: the budget. ``%(quiet_ms)d`` / ``%(budget_ms)d`` are filled in per probe.
_QUIET_PROBE = """
(async (quietMs, budgetMs) => {
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
  try {
    for (;;) {
      const now = performance.now();
      if (now - last >= quietMs) {
        return {quiet: true, waited_ms: Math.round(now - started), mutations, resources};
      }
      if (now >= deadline) {
        return {quiet: false, waited_ms: Math.round(now - started), mutations, resources};
      }
      const pause = Math.max(1, Math.min(quietMs - (now - last), deadline - now));
      await new Promise((resolve) => setTimeout(resolve, pause));
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
    it preceded. ``outcome``: ``settled`` (the document loaded and the page
    stayed quiet for the window, before the cap), ``capped`` (the cap ended
    the wait; ``waiting_for`` names what the wait had not yet seen settle, in
    the order it judges them) or ``unknown`` (the page's state could not be
    read — ``detail`` says why — and the capture went ahead at once).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    waited_ms: int = Field(ge=0)
    outcome: SettleOutcome
    waiting_for: list[SettlePending] = Field(default_factory=list)
    detail: str | None = None

    @property
    def capped(self) -> bool:
        """Whether the cap ended the wait."""
        return self.outcome == "capped"


class _QuietWatch(BaseModel):
    """What one in-page probe reported, or why there is no report."""

    quiet: bool = False
    waited_ms: int = 0
    mutations: int = 0
    resources: int = 0
    #: The probe's document went away mid-probe (a navigation): wait for the next one.
    document_gone: bool = False
    #: The probe could not run or did not answer; the page state is unknown.
    error: str | None = None


async def _bounded(reply: Awaitable[_T], timeout: float) -> _T:
    return await asyncio.wait_for(reply, max(timeout, 0.0))


def _gone(error: object) -> bool:
    text = str(error)
    return any(marker in text for marker in _DOCUMENT_GONE)


async def watch_quiet(
    cdp: CDPCommandProtocol,
    *,
    session_id: str | None,
    frame_id: str,
    quiet_ms: int,
    budget_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> _QuietWatch:
    """Wait, in the page, for a quiet window of *quiet_ms*; at most *budget_s*.

    Two commands on the tab's session: ``Page.createIsolatedWorld`` (the
    document's :data:`SETTLE_WORLD`, created once and then found by name) and
    one ``Runtime.evaluate`` awaiting the probe's promise, which resolves by
    itself at what is left of the budget. A reply that is not the probe's (a
    test double, a document that refused the world) is an ``error``; a
    document that went away during the probe is ``document_gone``; a page
    that does not answer by the end of the budget (its main thread busy, a
    dialog open) is a probe that never saw quiet.
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
        return _QuietWatch(quiet=False, waited_ms=int(budget_s * 1000))
    except Exception as exc:
        if _gone(exc):
            return _QuietWatch(document_gone=True)
        return _QuietWatch(error=f"the settle world could not be created: {exc}")
    context = (world or {}).get("executionContextId") if isinstance(world, dict) else None
    if not isinstance(context, int):
        return _QuietWatch(error="the settle world has no execution context")

    remaining = left()
    expression = _QUIET_PROBE % {"quiet_ms": quiet_ms, "budget_ms": int(remaining * 1000)}
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
        # The page's main thread never ran the probe to its end: not quiet.
        return _QuietWatch(quiet=False, waited_ms=int(budget_s * 1000))
    except Exception as exc:
        if _gone(exc):
            return _QuietWatch(document_gone=True)
        return _QuietWatch(error=f"the settle probe failed: {exc}")
    if not isinstance(reply, dict):
        return _QuietWatch(error="the settle probe returned no result")
    details = reply.get("exceptionDetails")
    if details:
        if _gone(json.dumps(details)):
            return _QuietWatch(document_gone=True)
        return _QuietWatch(error="the settle probe raised in the page")
    value = (reply.get("result") or {}).get("value")
    if not isinstance(value, dict) or not isinstance(value.get("quiet"), bool):
        return _QuietWatch(error="the settle probe returned no result")
    try:
        return _QuietWatch.model_validate(value)
    except ValueError:
        return _QuietWatch(error="the settle probe returned no result")


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
    as ``unknown`` and the capture goes ahead.
    """
    started = clock()
    deadline = started + max_wait_s

    def settle(
        outcome: SettleOutcome,
        waiting_for: list[SettlePending] | None = None,
        detail: str | None = None,
    ) -> PageSettle:
        return PageSettle(
            waited_ms=max(0, round((clock() - started) * 1000)),
            outcome=outcome,
            waiting_for=list(waiting_for or []),
            detail=detail,
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
            return settle("capped", ["load", "dom"])
        if load is not None and not await load.wait_loaded(deadline - clock(), budget_s=max_wait_s):
            return settle("capped", ["load", "dom"])
        if frame_id is None:
            return settle("unknown", detail="the tab's main frame is not known")
        remaining = deadline - clock()
        watch = await watch_quiet(
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
            return settle("capped", ["load", "dom"])
        if watch.error is not None:
            logger.debug("settle (sid=%s): %s; capturing now", session_id, watch.error)
            return settle("unknown", detail=watch.error)
        if watch.quiet:
            return settle("settled")
        return settle("capped", ["dom"])
    return settle("capped", ["load", "dom"])


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
    "SETTLE_RECORD_FRAGMENTS",
    "SETTLE_WORLD",
    "PageSettle",
    "SettleOutcome",
    "SettlePending",
    "recorded_settle",
    "wait_for_settle",
    "watch_quiet",
]
