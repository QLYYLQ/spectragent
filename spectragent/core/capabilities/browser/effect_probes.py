"""Bounded in-page probes that measure what one browser action did.

The browser's action effects (``effects.py``; ``docs/architecture_design.md``
§4.5, "动作效果") are measured by the code that executes the action, right
before and right after its own dispatch, so each fact belongs to that action.
This module holds the measurements every executor shares:

- :class:`ProbeBudget` — the action's measurement budget
  (``BrowserCapability.action_effect_timeout_s``). Only probe time counts; a
  probe that fails or runs past what is left returns ``"unknown"``, and a
  spent budget answers ``"unknown"`` without sending anything. A probe never
  raises into the action: measuring cannot change an action's outcome.
- :func:`element_at` — the element the page hit-tests at a viewport point,
  and whether it is a given target element or inside it.
- :func:`focused_element` — the element that has keyboard focus.
- :class:`FieldProbe` — the focused control before input, held by reference
  so its value can be compared after input without the value leaving the page.
- :func:`current_location` / :func:`history_entry_committed` — the URL of the
  tab's current navigation-history entry, now or once a history navigation
  committed; :func:`document_loaded` — whether its document finished loading;
  :func:`tab_facts` — which tab a session is, and its URL.
- :func:`selection_facts` — a dropdown's selected option, from the
  :data:`SELECT_FACTS_JS` reading the selection script takes itself.

Every script runs through ``Runtime.evaluate`` / ``Runtime.callFunctionOn`` in
the tab's session and reads only what it reports: a role and readable name
(never a field's value), offsets, and an in-page hash of a field's value that
is compared and then dropped. Focus and hit-testing follow open shadow roots
and same-origin frames; a frame of another site is reported as the frame
element (hit) or ``"unavailable"`` (focus).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from agent_cdp import CDPCommandProtocol

from spectragent.core.capabilities.browser.dom.sensitivity import is_sensitive_control
from spectragent.core.capabilities.browser.effects import (
    ElementFact,
    ElementFacts,
    Location,
    OptionFacts,
    TabFacts,
)
from spectragent.schemas.action import Unmeasured

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: The fact a probe that could not answer leaves behind.
UNKNOWN: Literal["unknown"] = "unknown"


class _NoAnswer:
    """What :meth:`ProbeBudget.run` returns for a probe that failed or ran out of time.

    A unique object rather than the string ``"unknown"``: a probe's own answer
    may be a string (an object id, a URL), and the two must never be confused.
    """

    def __repr__(self) -> str:
        return "NO_ANSWER"


#: The one :class:`_NoAnswer`; compare with ``is``.
NO_ANSWER = _NoAnswer()

#: ``facts(el)``: an element's role and readable name; never its value. Shared by
#: every script below, which embed it. The text of editable content
#: (``contenteditable``) is that editor's value — what was typed into it — so it
#: never names an element: an editor without a label is named ``""``.
ELEMENT_FACTS_JS = r"""
function facts(el) {
  if (!el || el.nodeType !== 1) return null;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  let role = (el.getAttribute('role') || '').trim().split(/\s+/)[0];
  if (!role) {
    if (tag === 'input') {
      role = ({checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button',
               reset: 'button', image: 'button', range: 'slider', search: 'searchbox',
               number: 'spinbutton'})[type] || 'textbox';
    } else if (tag === 'a') {
      role = el.hasAttribute('href') ? 'link' : 'generic';
    } else if (tag === 'select') {
      role = (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    } else if (el.isContentEditable && tag !== 'body') {
      role = 'textbox';
    } else {
      role = ({button: 'button', textarea: 'textbox', img: 'img', h1: 'heading', h2: 'heading',
               h3: 'heading', h4: 'heading', h5: 'heading', h6: 'heading', nav: 'navigation',
               main: 'main', header: 'banner', footer: 'contentinfo', form: 'form', ul: 'list',
               ol: 'list', li: 'listitem', table: 'table', tr: 'row', td: 'cell', th: 'columnheader',
               option: 'option', dialog: 'dialog', iframe: 'frame', frame: 'frame', video: 'video',
               audio: 'audio', label: 'label', summary: 'button', body: 'document',
               html: 'document', svg: 'img', canvas: 'canvas'})[tag] || 'generic';
    }
  }
  let name = el.getAttribute('aria-label') || '';
  const labelledby = el.getAttribute('aria-labelledby');
  if (!name && labelledby) {
    name = labelledby.split(/\s+/).map(id => {
      const ref = el.ownerDocument.getElementById(id);
      return ref ? ref.textContent : '';
    }).join(' ');
  }
  if (!name && el.labels && el.labels.length) {
    name = Array.from(el.labels).map(label => label.textContent).join(' ');
  }
  if (!name) {
    name = el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder') || '';
  }
  if (!name && tag === 'input' && ['button', 'submit', 'reset'].includes(type)) name = el.value || '';
  if (!name && !['input', 'textarea', 'select', 'body', 'html'].includes(tag)) {
    const walker = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const parts = [];
    let length = 0;
    while (length < 160 && walker.nextNode()) {
      const node = walker.currentNode;
      const host = node.parentElement;
      if (host && host.isContentEditable) continue;
      const parent = host ? host.tagName : '';
      if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(parent)) continue;
      const text = node.nodeValue.trim();
      if (text) { parts.push(text); length += text.length + 1; }
    }
    name = parts.join(' ');
  }
  name = name.replace(/\s+/g, ' ').trim();
  if (name.length > 80) name = name.slice(0, 77) + '...';
  return {role: role, name: name};
}
"""

#: ``deepFocus()``: ``{el}`` the focused element through open shadow roots and
#: same-origin frames, ``{opaque: true}`` inside a frame of another site,
#: ``{el: null}`` when nothing (or only the document) is focused.
_DEEP_FOCUS_JS = r"""
function deepFocus() {
  let el = document.activeElement;
  for (let hops = 0; el && hops < 64; hops += 1) {
    if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      let inner = null;
      try { inner = el.contentDocument; } catch (e) { inner = null; }
      if (!inner) return {opaque: true};
      el = inner.activeElement;
      continue;
    }
    break;
  }
  if (!el || el.tagName === 'BODY' || el.tagName === 'HTML') return {el: null};
  return {el: el};
}
"""

#: ``deepHit(x, y)``: the element hit-tested at a viewport point, through open
#: shadow roots and same-origin frames; a frame of another site is the frame.
_DEEP_HIT_JS = r"""
function deepHit(x, y) {
  let doc = document, el = document.elementFromPoint(x, y);
  for (let hops = 0; el && hops < 64; hops += 1) {
    if (el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (inner && inner !== el) { el = inner; continue; }
    }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      let inner = null;
      try { inner = el.contentDocument; } catch (e) { inner = null; }
      if (!inner) break;
      const box = el.getBoundingClientRect();
      x -= box.left + el.clientLeft; y -= box.top + el.clientTop;
      doc = inner; el = inner.elementFromPoint(x, y);
      continue;
    }
    break;
  }
  return el;
}
"""

#: Whether *node* is *target* or inside it, across shadow boundaries.
_INSIDE_JS = r"""
function inside(target, node) {
  for (let n = node, hops = 0; n && hops < 4096; hops += 1) {
    if (n === target) return true;
    n = n.parentNode || n.host || null;
  }
  return false;
}
"""

_HIT_EXPRESSION = (
    "(() => {" + ELEMENT_FACTS_JS + _DEEP_HIT_JS + "const el = deepHit(%(x)d, %(y)d);"
    " return el ? {facts: facts(el)} : {nothing: true}; })()"
)

_HIT_ON_TARGET_FN = (
    "function(x, y) {" + ELEMENT_FACTS_JS + _DEEP_HIT_JS + _INSIDE_JS + "const el = deepHit(x, y);"
    " return el ? {facts: facts(el), on_target: inside(this, el)} : {nothing: true}; }"
)

_FOCUS_EXPRESSION = (
    "(() => {" + ELEMENT_FACTS_JS + _DEEP_FOCUS_JS + "const f = deepFocus();"
    " if (f.opaque) return {opaque: true}; return f.el ? {facts: facts(f.el)} : {nothing: true}; })()"
)

#: The focused element itself (by reference), for :class:`FieldProbe`.
_FOCUSED_REF_EXPRESSION = (
    "(() => {" + _DEEP_FOCUS_JS + "const f = deepFocus(); return f.opaque ? 'opaque' : f.el; })()"
)

#: What :class:`FieldProbe` reads off its element: facts, the sensitivity
#: attributes, and a hash of the current value that never leaves as the value.
_FIELD_FN = (
    "function() {" + ELEMENT_FACTS_JS + "const el = this;"
    " const attrs = {};"
    " for (const n of ['type', 'autocomplete', 'id', 'name']) {"
    "   const v = el.getAttribute(n); if (v !== null) attrs[n] = v; }"
    " const value = ('value' in el && typeof el.value === 'string') ? el.value"
    "   : (el.isContentEditable ? (el.textContent || '') : null);"
    " let digest = null;"
    " if (value !== null) { let h = 2166136261;"
    "   for (let i = 0; i < value.length; i += 1) { h ^= value.charCodeAt(i);"
    "     h = Math.imul(h, 16777619) >>> 0; }"
    "   digest = value.length + ':' + h; }"
    " return {facts: facts(el), tag: el.tagName, attrs: attrs, digest: digest,"
    "   connected: el.isConnected}; }"
)


@dataclass
class ProbeBudget:
    """One action's measurement budget, in seconds of probe time.

    Built from ``action_effect_timeout_s`` per action execution. Time between
    probes (the action itself) is not counted.
    """

    seconds: float
    spent: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - self.spent)

    async def sleep(self, seconds: float) -> None:
        """Pause as part of a measurement (waiting for an effect to land), charged to the budget.

        Never pauses past what is left, so a probe that polls with it ends
        within the budget like any other probe.
        """
        pause = min(seconds, self.remaining)
        if pause <= 0:
            return
        started = time.monotonic()
        try:
            await asyncio.sleep(pause)
        finally:
            self.spent += time.monotonic() - started

    async def run(self, call: Callable[[], Awaitable[_T]], what: str) -> _T | _NoAnswer:
        """``await call()`` within what is left; :data:`NO_ANSWER` when it fails or runs out."""
        left = self.remaining
        if left <= 0:
            return NO_ANSWER
        started = time.monotonic()
        try:
            return await asyncio.wait_for(call(), left)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a probe never fails the action
            logger.debug("effect probe %s gave no answer: %r", what, exc)
            return NO_ANSWER
        finally:
            self.spent += time.monotonic() - started


def element_facts(value: Any) -> ElementFacts | None:
    """An :class:`ElementFacts` from a script's ``facts(el)`` reply, or ``None`` when malformed."""
    if not isinstance(value, dict):
        return None
    role, name = value.get("role"), value.get("name")
    if not isinstance(role, str) or not role or not isinstance(name, str):
        return None
    return ElementFacts(role=role, name=name)


def _value(reply: Any) -> Any:
    """The by-value result of an evaluation; raises on a JavaScript exception."""
    if not isinstance(reply, dict) or reply.get("exceptionDetails"):
        raise RuntimeError(f"evaluation failed: {reply!r:.200}")
    return (reply.get("result") or {}).get("value")


def _fact_from(value: Any) -> ElementFact:
    if not isinstance(value, dict):
        return UNKNOWN
    if value.get("opaque"):
        return "unavailable"
    if value.get("nothing"):
        return "nothing"
    return element_facts(value.get("facts")) or UNKNOWN


async def element_at(
    cdp: CDPCommandProtocol,
    session_id: str | None,
    x: int,
    y: int,
    budget: ProbeBudget,
    *,
    target_object_id: str | None = None,
) -> tuple[ElementFact, bool | Unmeasured | None]:
    """The element hit-tested at viewport point ``(x, y)``, and whether it is the target.

    With *target_object_id* (a remote object of the intended element, in the
    same session), the second value says whether the hit element is that
    element or inside it; without one it is ``None``.
    """

    async def by_point() -> dict[str, Any]:
        reply = await cdp.send(
            "Runtime.evaluate",
            {"expression": _HIT_EXPRESSION % {"x": x, "y": y}, "returnByValue": True},
            session_id=session_id,
        )
        return _value(reply)

    async def on_target() -> dict[str, Any]:
        reply = await cdp.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": _HIT_ON_TARGET_FN,
                "objectId": target_object_id,
                "arguments": [{"value": x}, {"value": y}],
                "returnByValue": True,
            },
            session_id=session_id,
        )
        return _value(reply)

    if target_object_id is None:
        answer = await budget.run(by_point, "hit test")
        return _fact_from(answer), None
    answer = await budget.run(on_target, "hit test on target")
    fact = _fact_from(answer)
    if not isinstance(answer, dict) or answer.get("nothing"):
        return fact, (UNKNOWN if fact == UNKNOWN else False)
    target = answer.get("on_target")
    return fact, target if isinstance(target, bool) else UNKNOWN


async def focused_element(
    cdp: CDPCommandProtocol, session_id: str | None, budget: ProbeBudget
) -> ElementFact:
    """The element with keyboard focus: ``"nothing"`` for the document itself,
    ``"unavailable"`` inside a frame of another site."""

    async def probe() -> Any:
        reply = await cdp.send(
            "Runtime.evaluate",
            {"expression": _FOCUS_EXPRESSION, "returnByValue": True},
            session_id=session_id,
        )
        return _value(reply)

    return _fact_from(await budget.run(probe, "focus"))


async def resolve_object(
    cdp: CDPCommandProtocol, session_id: str | None, backend_node_id: int, budget: ProbeBudget
) -> str | None:
    """A remote object id for a backend node (``DOM.resolveNode``), or ``None``."""

    async def probe() -> str | None:
        reply = await cdp.send(
            "DOM.resolveNode", {"backendNodeId": backend_node_id}, session_id=session_id
        )
        obj = (reply or {}).get("object") or {}
        object_id = obj.get("objectId")
        return object_id if isinstance(object_id, str) else None

    answer = await budget.run(probe, "resolve node")
    return answer if isinstance(answer, str) and answer is not NO_ANSWER else None


async def release_object(cdp: CDPCommandProtocol, session_id: str | None, object_id: str) -> None:
    """Release a remote object a probe held; a failure only means it was already gone."""
    try:
        await asyncio.wait_for(
            cdp.send("Runtime.releaseObject", {"objectId": object_id}, session_id=session_id), 1.0
        )
    except Exception as exc:
        logger.debug("releasing probe object failed: %r", exc)


@dataclass
class FieldProbe:
    """The control that has keyboard focus, read before input and again after it.

    :meth:`before` holds the focused element by remote-object reference and
    reads its facts, its sensitivity attributes and a hash of its value;
    :meth:`after` reads the hash again on the same element — wherever focus
    went meanwhile — and :meth:`release` drops the reference. The value
    itself never leaves the page.
    """

    cdp: CDPCommandProtocol
    session_id: str | None
    budget: ProbeBudget
    target: ElementFact = UNKNOWN
    sensitive: bool | Unmeasured = UNKNOWN
    _object_id: str | None = field(default=None, repr=False)
    _digest_before: str | None = field(default=None, repr=False)
    _has_value: bool = field(default=False, repr=False)

    async def before(self) -> None:
        async def focused_ref() -> Any:
            reply = await self.cdp.send(
                "Runtime.evaluate",
                {"expression": _FOCUSED_REF_EXPRESSION, "returnByValue": False},
                session_id=self.session_id,
            )
            if not isinstance(reply, dict) or reply.get("exceptionDetails"):
                raise RuntimeError(f"evaluation failed: {reply!r:.200}")
            return reply.get("result") or {}

        result = await self.budget.run(focused_ref, "focused field")
        if not isinstance(result, dict):
            return
        if result.get("value") == "opaque":
            self.target = self.sensitive = "unavailable"
            return
        object_id = result.get("objectId")
        if not isinstance(object_id, str):
            if result.get("subtype") == "null":
                # Nothing is focused; the keys go to the page itself.
                self.target, self.sensitive = "nothing", False
            # Anything else is a reply that does not say: left unknown.
            return
        self._object_id = object_id
        reading = await self._read()
        if not isinstance(reading, dict):
            return
        self.target = element_facts(reading.get("facts")) or UNKNOWN
        attrs = reading.get("attrs")
        self.sensitive = is_sensitive_control(
            str(reading.get("tag") or ""),
            {str(k).lower(): str(v) for k, v in attrs.items()} if isinstance(attrs, dict) else {},
        )
        digest = reading.get("digest")
        self._has_value = isinstance(digest, str)
        self._digest_before = digest if isinstance(digest, str) else None

    async def after(self) -> bool | Unmeasured:
        """Whether the value changed: unmeasured when the field had no value or could not be read."""
        if self.target == "nothing" or self.target == "unavailable":
            return "unavailable"
        if self._object_id is None or self._digest_before is None:
            return "unavailable" if self.target != UNKNOWN and not self._has_value else UNKNOWN
        reading = await self._read()
        if not isinstance(reading, dict) or not reading.get("connected"):
            return UNKNOWN
        digest = reading.get("digest")
        return digest != self._digest_before if isinstance(digest, str) else UNKNOWN

    async def release(self) -> None:
        if self._object_id is not None:
            await release_object(self.cdp, self.session_id, self._object_id)
            self._object_id = None

    async def _read(self) -> Any:
        object_id = self._object_id

        async def read() -> Any:
            reply = await self.cdp.send(
                "Runtime.callFunctionOn",
                {"functionDeclaration": _FIELD_FN, "objectId": object_id, "returnByValue": True},
                session_id=self.session_id,
            )
            return _value(reply)

        return await self.budget.run(read, "field reading")


# ── Navigation, tabs and selection ────────────────────────────────────

#: How often :func:`history_entry_committed` asks again while it waits.
HISTORY_POLL_S = 0.02


def _current_entry(history: Any) -> dict[str, Any] | None:
    """The current entry of a ``Page.getNavigationHistory`` reply, or ``None`` when malformed."""
    if not isinstance(history, dict):
        return None
    index, entries = history.get("currentIndex"), history.get("entries")
    if not isinstance(index, int) or not isinstance(entries, list):
        return None
    if not 0 <= index < len(entries) or not isinstance(entries[index], dict):
        return None
    return entries[index]


def history_location(history: Any) -> Location | None:
    """The URL of the current entry of a ``Page.getNavigationHistory`` reply, or ``None``.

    The current entry is the tab's last committed one: the main frame's
    document, the fragment included (a same-document navigation updates it).
    A navigation still in flight is not in it yet.
    """
    entry = _current_entry(history)
    url = entry.get("url") if entry is not None else None
    return Location(url=url) if isinstance(url, str) else None


async def _history(cdp: CDPCommandProtocol, session_id: str | None) -> Any:
    return await cdp.send("Page.getNavigationHistory", {}, session_id=session_id)


async def current_location(
    cdp: CDPCommandProtocol, session_id: str | None, budget: ProbeBudget
) -> Location | Literal["unknown"]:
    """The URL of the tab's current (last committed) navigation-history entry."""

    async def probe() -> Location:
        location = history_location(await _history(cdp, session_id))
        if location is None:
            raise RuntimeError("no current navigation-history entry")
        return location

    answer = await budget.run(probe, "location")
    return answer if isinstance(answer, Location) else UNKNOWN


async def history_entry_committed(
    cdp: CDPCommandProtocol, session_id: str | None, entry_id: int, budget: ProbeBudget
) -> Location | Literal["unknown"]:
    """Wait within the budget until history entry *entry_id* is the tab's current one; its URL.

    ``Page.navigateToHistoryEntry`` answers before the navigation commits,
    and the history's current entry changes only when it does. ``"unknown"``
    when it has not become current by the end of the budget: the tab's URL
    then is not the one the history navigation went to.
    """

    async def probe() -> Location:
        while True:
            history = await _history(cdp, session_id)
            entry = _current_entry(history)
            if entry is not None and entry.get("id") == entry_id:
                location = history_location(history)
                if location is None:
                    raise RuntimeError("the committed history entry has no URL")
                return location
            await asyncio.sleep(HISTORY_POLL_S)

    answer = await budget.run(probe, "history entry")
    return answer if isinstance(answer, Location) else UNKNOWN


async def document_loaded(
    cdp: CDPCommandProtocol, session_id: str | None, budget: ProbeBudget
) -> bool | Literal["unknown"]:
    """Whether the tab's document has finished loading (``document.readyState == "complete"``)."""

    async def probe() -> bool:
        reply = await cdp.send(
            "Runtime.evaluate",
            {"expression": "document.readyState", "returnByValue": True},
            session_id=session_id,
        )
        state = _value(reply)
        if not isinstance(state, str):
            raise RuntimeError(f"no document state: {state!r:.80}")
        return state == "complete"

    answer = await budget.run(probe, "document state")
    return answer if isinstance(answer, bool) else UNKNOWN


async def tab_facts(
    cdp: CDPCommandProtocol,
    session_id: str | None,
    budget: ProbeBudget,
    *,
    tab_id: str | None = None,
) -> TabFacts | Literal["unknown"]:
    """The tab a control session is attached to, and its current URL.

    ``"unknown"`` only when the tab itself cannot be named; a URL that cannot
    be read leaves the tab's ``location`` unknown and its identifier known.

    *tab_id* is the tab's CDP target id when the caller already knows it;
    otherwise ``Target.getTargetInfo`` on the session names it — the
    ``tab_id`` ``browser_meta`` lists. The URL is the current history entry's
    (:func:`current_location`), the same source a navigation's effect reads.
    """
    if tab_id is None:

        async def probe() -> str:
            reply = await cdp.send("Target.getTargetInfo", {}, session_id=session_id)
            info = (reply or {}).get("targetInfo") or {}
            target = info.get("targetId")
            if not isinstance(target, str) or not target:
                raise RuntimeError(f"no target id: {reply!r:.200}")
            return target

        answer = await budget.run(probe, "tab id")
        if answer is NO_ANSWER or not isinstance(answer, str):
            return UNKNOWN
        tab_id = answer
    location = await current_location(cdp, session_id, budget)
    # A known tab keeps its identifier when only its URL could not be read.
    return TabFacts(tab_id=tab_id, location=location if isinstance(location, Location) else UNKNOWN)


#: ``selectFacts(el, withhold)``: a ``<select>``'s selected index, that option's
#: visible text (``null`` when *withhold*: the text stays in the page), and the
#: tag and attributes the sensitivity rule reads — what :func:`selection_facts`
#: takes. Embedded by the selection script, which calls it right after selecting.
SELECT_FACTS_JS = r"""
function selectFacts(el, withhold) {
  const attrs = {};
  for (const n of ['type', 'autocomplete', 'id', 'name']) {
    const v = el.getAttribute(n); if (v !== null) attrs[n] = v; }
  const shown = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
  return {index: el.selectedIndex, text: (withhold || !shown) ? null : shown.text,
          tag: el.tagName, attrs: attrs};
}
"""


def selection_facts(
    before: Any, after: Any, *, withheld: bool
) -> dict[str, OptionFacts | bool | Unmeasured]:
    """A :class:`~spectragent.core.capabilities.browser.effects.SelectionEffect`'s facts.

    *before* is the ``selectedIndex`` read right before the selection,
    *after* a :data:`SELECT_FACTS_JS` reading right after it. *withheld*: the
    caller judged the control sensitive already and the page kept the text;
    the reading's own tag and attributes are judged again by the shared rule
    (``dom/sensitivity.py``), so a control the page made sensitive since the
    observation also has its text dropped. A reading that cannot be judged
    leaves the selected option unknown rather than quoting it.
    """
    reading: dict[str, Any] = after if isinstance(after, dict) else {}
    index = _index(reading.get("index"))
    selected: OptionFacts | Literal["unknown"] = UNKNOWN
    tag, attrs, text = reading.get("tag"), reading.get("attrs"), reading.get("text")
    if index is not None and index >= 0 and isinstance(tag, str) and isinstance(attrs, dict):
        sensitive = withheld or is_sensitive_control(
            tag, {str(k).lower(): str(v) for k, v in attrs.items()}
        )
        if sensitive:
            selected = OptionFacts(index=index, text=None)
        elif isinstance(text, str):
            selected = OptionFacts(index=index, text=text)
    first = _index(before)
    changed: bool | Literal["unknown"] = (
        first != index if first is not None and index is not None else UNKNOWN
    )
    return {"selected": selected, "value_changed": changed}


def _index(value: Any) -> int | None:
    """A ``selectedIndex`` from a script's reply (``-1``: nothing selected), or ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = [
    "ELEMENT_FACTS_JS",
    "HISTORY_POLL_S",
    "NO_ANSWER",
    "SELECT_FACTS_JS",
    "UNKNOWN",
    "FieldProbe",
    "ProbeBudget",
    "current_location",
    "document_loaded",
    "element_at",
    "element_facts",
    "focused_element",
    "history_entry_committed",
    "history_location",
    "release_object",
    "resolve_object",
    "selection_facts",
    "tab_facts",
]
