"""Measuring one ``browser.scroll``: which container moved, how far, and whether it hit an edge.

A wheel event scrolls whatever the page routes it to: the innermost scroll
box under the point that can still move that way, else the next one out,
else the document. The pointer's place decides it, so a scroll dispatched
with the pointer parked in a corner can move the page or nothing, and a
screenshot after a batch cannot tell which (the 2026-10-19 recording that
scrolled from a pointer at (45, 34) did move the page, by offsets the
observations never showed the model).

:class:`ScrollProbe` therefore holds, by reference, every container that
could take the wheel at its point — the scroll boxes from the hit-tested
element outwards, through open shadow roots and same-origin frames, then each
document's scrolling element — reads their offsets before the wheel, waits
after it until the offsets stop changing (smooth scrolling animates over
several frames), and reports the innermost container that moved. When none
moved it reports the one the wheel was aimed at: with a measured ``0``
displacement only when every held container was already at its edge in that
direction, so no move could come. When one still had room and no change was
seen before the budget ran out, the wheel may simply not have landed yet (a
first wheel was measured landing ~0.9 s late), so the position after and the
edge are ``"unknown"`` — never a measured zero. A point over a frame of
another site, where nothing on this side moved, is ``"unavailable"``.
Everything is bounded by the action's
:class:`~spectragent.core.capabilities.browser.effect_probes.ProbeBudget`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_cdp import CDPCommandProtocol

from spectragent.core.capabilities.browser.effect_probes import (
    DEEP_HIT_JS,
    ELEMENT_FACTS_JS,
    NO_ANSWER,
    UNKNOWN,
    ProbeBudget,
    element_facts,
    release_object,
)
from spectragent.core.capabilities.browser.effects import ElementFacts, ScrollOffset

logger = logging.getLogger(__name__)

Direction = Literal["up", "down", "left", "right"]

#: How long the offsets must stay put before the scroll counts as finished.
_STABLE_INTERVAL_S = 0.05
#: How often the offsets are read while waiting for the wheel to land.
_CHANGE_POLL_S = 0.015
#: Budget kept back after each pause for the read that follows it.
_READ_RESERVE_S = 0.02
#: The most reads after the wheel; the budget usually ends it first.
_MAX_READS = 200

#: The containers the wheel at ``(x, y)`` could move, innermost first, held by
#: reference: ``{chain: [elements], frame: <the point is over another site's frame>}``.
_CHAIN_EXPRESSION = (
    "((x, y) => {"
    + DEEP_HIT_JS
    + r"""
  function scrolls(n) {
    const doc = n.ownerDocument;
    if (n === doc.scrollingElement || n === doc.documentElement || n === doc.body) return false;
    const cs = doc.defaultView.getComputedStyle(n);
    const open = v => v === 'auto' || v === 'scroll' || v === 'overlay';
    return (open(cs.overflowY) && n.scrollHeight > n.clientHeight)
        || (open(cs.overflowX) && n.scrollWidth > n.clientWidth);
  }
  const chain = [];
  const hit = deepHit(x, y);
  let n = hit.el;
  for (let hops = 0; hops < 4096; hops += 1) {
    if (!n) break;
    if (n.nodeType === 1 && scrolls(n)) chain.push(n);
    if (n.parentNode && n.parentNode.nodeType !== 9) { n = n.parentNode.host || n.parentNode; continue; }
    if (n.parentNode && n.parentNode.nodeType === 11) { n = n.parentNode.host; continue; }
    const owner = (n.ownerDocument || n).defaultView;
    const docNode = n.ownerDocument || n;
    chain.push(docNode.scrollingElement || docNode.documentElement);
    let up = null;
    try { up = owner && owner.frameElement; } catch (e) { up = null; }
    n = up;
  }
  if (!chain.length) chain.push(document.scrollingElement || document.documentElement);
  return {chain: chain, frame: hit.opaque};
})(%(x)d, %(y)d)
"""
)

#: The offsets of every held container, and (when asked) the facts of each.
_READ_FN = (
    "function(withFacts) {"
    + ELEMENT_FACTS_JS
    + " return {frame: this.frame, items: this.chain.map(n => {"
    "   const doc = n.ownerDocument;"
    "   const page = n === (doc.scrollingElement || doc.documentElement);"
    "   return {x: Math.round(n.scrollLeft), y: Math.round(n.scrollTop),"
    "     max_x: Math.max(0, n.scrollWidth - n.clientWidth),"
    "     max_y: Math.max(0, n.scrollHeight - n.clientHeight),"
    "     page: page && doc === document, connected: n.isConnected,"
    "     facts: withFacts && !page ? facts(n) : null}; })}; }"
)


@dataclass(frozen=True)
class _Reading:
    x: int
    y: int
    max_x: int
    max_y: int
    page: bool
    facts: ElementFacts | None


def _readings(value: Any) -> tuple[bool, list[_Reading]] | None:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return None
    items: list[_Reading] = []
    for item in value["items"]:
        if not isinstance(item, dict) or not item.get("connected", True):
            return None
        try:
            items.append(
                _Reading(
                    x=int(item["x"]),
                    y=int(item["y"]),
                    max_x=int(item["max_x"]),
                    max_y=int(item["max_y"]),
                    page=bool(item.get("page")),
                    facts=element_facts(item.get("facts")),
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    return bool(value.get("frame")), items


def _vertical(direction: Direction) -> bool:
    return direction in ("up", "down")


def _can_move(reading: _Reading, direction: Direction) -> bool:
    """Whether the container has room to move that way at all (its scroll range)."""
    return (reading.max_y if _vertical(direction) else reading.max_x) > 0


def _at_edge(reading: _Reading, direction: Direction) -> bool:
    if direction == "down":
        return reading.y >= reading.max_y - 1
    if direction == "up":
        return reading.y <= 0
    if direction == "right":
        return reading.x >= reading.max_x - 1
    return reading.x <= 0


@dataclass
class ScrollProbe:
    """One scroll's measurement: :meth:`before` the wheel, :meth:`after` it, :meth:`release`."""

    cdp: CDPCommandProtocol
    session_id: str | None
    budget: ProbeBudget
    direction: Direction
    _object_id: str | None = field(default=None, repr=False)
    _before: list[_Reading] | None = field(default=None, repr=False)
    _frame: bool = field(default=False, repr=False)

    async def before(self, x: int, y: int) -> None:
        """Hold the containers the wheel at ``(x, y)`` could move, and read their offsets."""

        async def chain() -> str | None:
            reply = await self.cdp.send(
                "Runtime.evaluate",
                {"expression": _CHAIN_EXPRESSION % {"x": x, "y": y}, "returnByValue": False},
                session_id=self.session_id,
            )
            if not isinstance(reply, dict) or reply.get("exceptionDetails"):
                raise RuntimeError(f"evaluation failed: {reply!r:.200}")
            object_id = (reply.get("result") or {}).get("objectId")
            return object_id if isinstance(object_id, str) else None

        object_id = await self.budget.run(chain, "scroll containers")
        if object_id is NO_ANSWER or not isinstance(object_id, str):
            return
        self._object_id = object_id
        reading = await self._read(with_facts=True)
        if reading is not None:
            self._frame, self._before = reading

    async def after(self) -> dict[str, Any]:
        """The :class:`~spectragent.core.capabilities.browser.effects.ScrollEffect` facts.

        A wheel lands asynchronously: usually within a few tens of
        milliseconds, but the first one on a freshly loaded page was measured
        landing ~0.9 s after dispatch. So when some held container still has
        room in that direction, the offsets are polled until one changes (or
        the budget ends), then until two reads :data:`_STABLE_INTERVAL_S`
        apart agree. When every container is already at its edge nothing is
        waited for beyond one interval. The waiting is charged to the budget,
        so the whole measurement ends within it. A move seen before the
        budget ends is reported from the last read; when a container had room
        and nothing moved by then, the position after and the edge are
        unknown (:meth:`_choose`).
        """
        facts: dict[str, Any] = {
            "direction": self.direction,
            "container": UNKNOWN,
            "before": UNKNOWN,
            "after": UNKNOWN,
            "at_boundary": UNKNOWN,
        }
        before = self._before
        if before is None:
            return facts
        offsets = [(r.x, r.y) for r in before]
        room = any(_can_move(r, self.direction) and not _at_edge(r, self.direction) for r in before)
        last = await self._read(with_facts=False)
        reads = 1
        if last is None:
            return facts
        # Each pause leaves room for one more read, so the last reading taken
        # is never lost to a read the spent budget refused.
        if room:
            while (
                [(r.x, r.y) for r in last[1]] == offsets
                and self.budget.remaining > _CHANGE_POLL_S + _READ_RESERVE_S
                and reads < _MAX_READS
            ):
                await self.budget.sleep(_CHANGE_POLL_S)
                current = await self._read(with_facts=False)
                reads += 1
                if current is None:
                    break
                last = current
        while self.budget.remaining > _STABLE_INTERVAL_S + _READ_RESERVE_S and reads < _MAX_READS:
            await self.budget.sleep(_STABLE_INTERVAL_S)
            current = await self._read(with_facts=False)
            reads += 1
            if current is None:
                break
            settled = [(r.x, r.y) for r in current[1]] == [(r.x, r.y) for r in last[1]]
            last = current
            if settled:
                break
        if len(last[1]) != len(before):
            return facts
        return self._choose(before, last[1], frame=self._frame, room=room)

    async def release(self) -> None:
        if self._object_id is not None:
            await release_object(self.cdp, self.session_id, self._object_id)
            self._object_id = None

    def _choose(
        self, before: list[_Reading], after: list[_Reading], *, frame: bool, room: bool
    ) -> dict[str, Any]:
        """The facts of the container that moved, or of the one the wheel was aimed at.

        *room*: some held container could still move in this direction before
        the wheel. Then nothing having moved is not a measured zero — the wheel
        may land after the budget — and the position after and the edge are
        unknown; the container and its position before are still measured.
        """
        moved = [
            i
            for i, (b, a) in enumerate(zip(before, after, strict=True))
            if (b.x, b.y) != (a.x, a.y)
        ]
        if moved:
            index = moved[0]
        elif frame:
            return {
                "direction": self.direction,
                "container": "unavailable",
                "before": "unavailable",
                "after": "unavailable",
                "at_boundary": "unavailable",
            }
        else:
            # Nothing moved: report the container the wheel was aimed at — the
            # innermost one with any room on this axis, else the document.
            index = next(
                (i for i, reading in enumerate(before) if _can_move(reading, self.direction)),
                len(before) - 1,
            )
        b, a = before[index], after[index]
        unseen = not moved and room
        return {
            "direction": self.direction,
            "container": "page" if b.page else "element",
            "element": None if b.page else b.facts,
            "before": ScrollOffset(x=b.x, y=b.y),
            "after": UNKNOWN if unseen else ScrollOffset(x=a.x, y=a.y),
            "at_boundary": UNKNOWN if unseen else _at_edge(a, self.direction),
        }

    async def _read(self, *, with_facts: bool) -> tuple[bool, list[_Reading]] | None:
        object_id = self._object_id
        if object_id is None:
            return None

        async def read() -> Any:
            reply = await self.cdp.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": _READ_FN,
                    "objectId": object_id,
                    "arguments": [{"value": with_facts}],
                    "returnByValue": True,
                },
                session_id=self.session_id,
            )
            if not isinstance(reply, dict) or reply.get("exceptionDetails"):
                raise RuntimeError(f"evaluation failed: {reply!r:.200}")
            return (reply.get("result") or {}).get("value")

        value = await self.budget.run(read, "scroll offsets")
        return None if value is NO_ANSWER else _readings(value)


__all__ = ["ScrollProbe"]
