"""A tab whose effect probes answer from a script, for the action-effect unit tests.

The action runs for real: :class:`EffectTab` is a capability lookalike whose
focused tab is a real agent-cdp scope with a real ``ActionWatchdog`` attached,
so an action's event reaches its actual handler and the action builds its
``Effect`` from the facts that handler measured. Only the browser is fake:
:class:`ProbeCDP` records every CDP call and answers each effect probe of
``effect_probes.py`` from a script — recognised by the function it runs, not by
the order of the calls, so a test says *what the page answers* and the
handler decides when to ask.

Probe kinds: ``hit`` (the hit test, by point or on the target), ``focus``
(the focused element), ``field_ref`` / ``field_read`` (``FieldProbe``: the
focused control by reference, then its readings), ``describe``
(``DOM.describeNode``: whether that control hosts a closed shadow root),
``sensitivity`` (the typing handler's own focused-field read), ``rect`` (the
live box of a page element), ``resolve`` (``DOM.resolveNode``) and
``release``. A kind in ``fail`` raises; a kind in ``hang`` never answers, so
only the measurement budget ends it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from agent_cdp import EventScope, ScopeGroup

from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMRect
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
    _FOCUSED_FIELD_JS,
    ActionWatchdog,
)
from tests.test_core.test_capabilities.test_browser_actions.conftest import FakeElementNode

#: The live box every page element reports: centre (55, 70).
RECT = {"x": 50.0, "y": 60.0, "w": 10.0, "h": 20.0}
CENTRE = (55, 70)


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    session_id: str | None


def facts(role: str, name: str = "") -> dict[str, Any]:
    """What ``facts(el)`` answers for an element."""
    return {"role": role, "name": name}


def reading(
    role: str,
    name: str,
    *,
    digest: str | None,
    tag: str = "INPUT",
    entry: str | None = "text",
    **attrs: str,
) -> dict[str, Any]:
    """One ``FieldProbe`` reading: facts, sensitivity attributes, text entry, and a value hash.

    *entry* is what ``textEntry(el)`` answers: ``"text"`` (the default, a text
    field), ``"widget"``, ``"read-only"``, ``"none"``, ``"no-role"`` (then the
    ``describe`` probe decides), or ``None`` (the page cannot tell).
    """
    return {
        "facts": facts(role, name),
        "tag": tag,
        "attrs": attrs,
        "entry": entry,
        "digest": digest,
        "connected": True,
    }


def probe_kind(method: str, params: dict[str, Any]) -> str | None:
    """Which effect probe (or measuring call) a CDP call is; ``None`` for input and the rest."""
    if method == "Runtime.evaluate":
        expression = params.get("expression", "")
        if "deepHit" in expression:
            return "hit"
        if expression == _FOCUSED_FIELD_JS:
            return "sensitivity"
        if "deepFocus" in expression:
            return "field_ref" if params.get("returnByValue") is False else "focus"
        return None
    if method == "Runtime.callFunctionOn":
        function = params.get("functionDeclaration", "")
        if "deepHit" in function:
            return "hit"
        if "digest" in function:
            return "field_read"
        if "getBoundingClientRect" in function:
            return "rect"
        return None
    return {
        "DOM.resolveNode": "resolve",
        "DOM.describeNode": "describe",
        "Runtime.releaseObject": "release",
    }.get(method)


class ProbeCDP:
    """Records every call; answers the effect probes from the script given.

    ``hit``: the hit test's value (``{"facts": ..., "on_target": ...}`` or
    ``{"nothing": True}``). ``focus``: the focus read's value. ``field``:
    the focused control by reference — ``"node"`` (then ``readings``, one per
    read, the last repeated), ``"null"``, ``"opaque"``, or ``"fail"`` /
    ``"hang"`` (that read raises / never answers); a sequence of them is one
    per reference read, the last repeated (focus that lands late).
    ``shadow_root``: the type of the shadow root ``DOM.describeNode`` lists on
    that control (``"closed"``, ``"open"``), or ``None`` for none.
    ``fail_methods``: CDP methods that raise, as a closed target's would.
    """

    def __init__(
        self,
        *,
        hit: dict[str, Any] | None = None,
        focus: dict[str, Any] | None = None,
        field: str | Iterable[str] = "null",
        readings: Iterable[dict[str, Any]] = (),
        fail: Iterable[str] = (),
        hang: Iterable[str] = (),
        fail_methods: Iterable[str] = (),
        shadow_root: str | None = None,
    ) -> None:
        self.calls: list[Call] = []
        self.shadow_root = shadow_root
        self.hit = hit if hit is not None else {"nothing": True}
        self.focus = focus if focus is not None else {"nothing": True}
        self.fields = [field] if isinstance(field, str) else list(field)
        self.field = self.fields[0]
        self.readings = list(readings)
        self.fail = set(fail)
        self.hang = set(hang)
        self.fail_methods = set(fail_methods)

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        params = params or {}
        self.calls.append(Call(method, params, session_id))
        kind = probe_kind(method, params)
        if method in self.fail_methods or kind in self.fail:
            raise RuntimeError(f"injected failure: {method}")
        if kind in self.hang:
            await asyncio.Event().wait()
        if kind == "hit":
            return {"result": {"type": "object", "value": self.hit}}
        if kind == "focus":
            return {"result": {"type": "object", "value": self.focus}}
        if kind == "field_ref":
            self.field = self.fields.pop(0) if len(self.fields) > 1 else self.fields[0]
            if self.field == "fail":
                raise RuntimeError("injected failure: the focus read")
            if self.field == "hang":
                await asyncio.Event().wait()
            if self.field == "node":
                return {"result": {"type": "object", "subtype": "node", "objectId": "field"}}
            if self.field == "opaque":
                return {"result": {"type": "string", "value": "opaque"}}
            return {"result": {"type": "object", "subtype": "null", "value": None}}
        if kind == "field_read":
            value = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
            return {"result": {"type": "object", "value": value}}
        if kind == "sensitivity":
            value = self.readings[0] if self.readings and self.field == "node" else None
            if value is None:
                return {"result": {"type": "object", "subtype": "null", "value": None}}
            return {
                "result": {
                    "type": "object",
                    "value": {"tag": value["tag"], "attrs": value["attrs"]},
                }
            }
        if kind == "rect":
            return {"result": {"type": "object", "value": dict(RECT)}}
        if kind == "resolve":
            return {"object": {"objectId": "target"}}
        if kind == "describe":
            roots = [{"shadowRootType": self.shadow_root}] if self.shadow_root else []
            return {"node": {"nodeType": 1, "localName": "div", "shadowRoots": roots}}
        if method == "DOM.getContentQuads":
            x, y, w, h = RECT["x"], RECT["y"], RECT["w"], RECT["h"]
            return {"quads": [[x, y, x + w, y, x + w, y + h, x, y + h]]}
        return {}

    def kinds(self) -> list[str]:
        """Every call as its probe kind, or its method when it is not a probe."""
        return [probe_kind(c.method, c.params) or c.method for c in self.calls]

    def inputs(self) -> list[Call]:
        return [c for c in self.calls if c.method.startswith("Input.")]

    def index(self, kind: str, *, last: bool = False) -> int:
        """Position of the first (or last) call of a probe kind, or of a mouse event type."""
        hits = [
            i
            for i, c in enumerate(self.calls)
            if (probe_kind(c.method, c.params) or c.method) == kind or c.params.get("type") == kind
        ]
        assert hits, f"no {kind} call in {self.kinds()}"
        return hits[-1] if last else hits[0]


def page_node(backend_node_id: int = 42) -> DOMElementNode:
    """An element of the tab's own document."""
    return DOMElementNode(
        tag_name="button",
        backend_node_id=backend_node_id,
        bounds=DOMRect(x=400.0, y=500.0, width=100.0, height=40.0),
        text_content="Go",
    )


def frame_node(backend_node_id: int = 52) -> DOMElementNode:
    """An element inside a same-process ``<iframe>`` of the tab."""
    owner = DOMElementNode(tag_name="iframe", backend_node_id=15)
    document = DOMElementNode(tag_name="#document", node_type=9, parent=owner)
    owner.content_document = document
    element = DOMElementNode(
        tag_name="button",
        backend_node_id=backend_node_id,
        bounds=DOMRect(x=10.0, y=10.0, width=60.0, height=20.0),
        text_content="Inside",
        parent=document,
    )
    document.children.append(element)
    return element


class EffectTab:
    """A capability lookalike whose focused tab runs a real ``ActionWatchdog``.

    ``selector_map`` is what the observation indexed (index → node); the
    action reads it to address the element, the watchdog to measure it.
    """

    def __init__(self, cdp: ProbeCDP, scope: EventScope, nodes: dict[int, DOMElementNode]) -> None:
        self.cdp = cdp
        self.default_session_id = "tab"
        self._scope = scope
        self._nodes = nodes

    @property
    def action_scope(self) -> EventScope:
        return self._scope

    @property
    def selector_map(self) -> dict[int, FakeElementNode]:
        return {
            index: FakeElementNode(node.backend_node_id, node.session_id)
            for index, node in self._nodes.items()
        }


@asynccontextmanager
async def effect_tab(
    cdp: ProbeCDP,
    *,
    nodes: dict[int, DOMElementNode] | None = None,
    platform: InputPlatform = "linux",
    **watchdog_kwargs: Any,
) -> AsyncIterator[EffectTab]:
    """A focused tab named ``tab`` whose actions run on *cdp*."""
    selector_map = dict(nodes or {})
    group = ScopeGroup("action-effects")
    scope = await group.create_scope("tab")
    watchdog = ActionWatchdog(
        cdp,  # type: ignore[arg-type]
        input_platform=platform,
        selector_map_getter=lambda: selector_map,
        **watchdog_kwargs,
    )
    watchdog.attach(scope, "tab")
    try:
        yield EffectTab(cdp, scope, selector_map)
    finally:
        await group.close_all()
