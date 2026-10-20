"""Verify ActionWatchdog click flow uses live getBoundingClientRect.

The legacy implementation fed ``node.bounds.center`` (document-space,
DPR-multiplied) directly to ``Input.dispatchMouseEvent`` (which expects
viewport-space CSS pixels). This caused silent click misses on Retina
or scrolled pages. The new flow runs scrollIntoViewIfNeeded → resolveNode
→ callFunctionOn(getBoundingClientRect) and uses the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMRect,
    DOMSelectorMap,
)
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
    ActionWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.events import ClickElementEvent


@dataclass
class _Call:
    method: str
    params: dict[str, Any]


class FakeCDP:
    """Records every ``send`` call and returns canned responses."""

    def __init__(
        self,
        *,
        rect: dict[str, float] | None = None,
        raise_on: set[str] | None = None,
    ) -> None:
        self.calls: list[_Call] = []
        self._rect = rect or {"x": 50.0, "y": 60.0, "w": 10.0, "h": 20.0}
        self._raise_on = raise_on or set()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(_Call(method=method, params=params or {}))
        if method in self._raise_on:
            raise RuntimeError(f"injected failure for {method}")
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-1"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": dict(self._rect)}}
        return {}


def _make_node(backend_node_id: int, bounds: DOMRect) -> DOMElementNode:
    return DOMElementNode(
        tag_name="a",
        backend_node_id=backend_node_id,
        bounds=bounds,
        text_content="link",
    )


def _outcome(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """The handler's answer without the effect it measured (``effect_facts``).

    What the click measured is pinned in ``test_browser_actions/test_pointer_effects.py``;
    these tests are about where it pressed.
    """
    if result is None:
        return None
    return {key: value for key, value in result.items() if key != "effect_facts"}


def _watchdog(
    cdp: FakeCDP,
    selector_map: DOMSelectorMap,
) -> ActionWatchdog:
    return ActionWatchdog(
        input_platform="linux",
        cdp=cdp,  # type: ignore[arg-type]
        selector_map_getter=lambda: selector_map,
    )


class TestClickUsesLiveRect:
    async def test_call_sequence_and_dispatched_coords(self) -> None:
        node = _make_node(42, DOMRect(x=400.0, y=800.0, width=100.0, height=40.0))
        cdp = FakeCDP(rect={"x": 50.0, "y": 60.0, "w": 10.0, "h": 20.0})
        wd = _watchdog(cdp, {42: node})

        result = await wd._on_click_element(ClickElementEvent(backend_node_id=42))
        assert _outcome(result) == {"clicked": True, "x": 55, "y": 70}

        methods = [c.method for c in cdp.calls]
        # Three live-rect calls, then the mouse events. The effect probes
        # around the press (hit test, focus) are interleaved with the mouse
        # events and are not part of this sequence.
        assert methods[:3] == [
            "DOM.scrollIntoViewIfNeeded",
            "DOM.resolveNode",
            "Runtime.callFunctionOn",
        ]

        # All three mouse events use the live-rect center (55, 70),
        # NOT node.bounds.center (450, 820).
        mouse_events = [c for c in cdp.calls if c.method == "Input.dispatchMouseEvent"]
        assert len(mouse_events) == 3
        assert {evt.params["type"] for evt in mouse_events} == {
            "mouseMoved",
            "mousePressed",
            "mouseReleased",
        }
        for evt in mouse_events:
            assert (evt.params["x"], evt.params["y"]) == (55, 70)

    async def test_scroll_into_view_targets_correct_backend_node(self) -> None:
        node = _make_node(42, DOMRect(width=10, height=10))
        cdp = FakeCDP()
        wd = _watchdog(cdp, {42: node})

        await wd._on_click_element(ClickElementEvent(backend_node_id=42))

        assert cdp.calls[0].method == "DOM.scrollIntoViewIfNeeded"
        assert cdp.calls[0].params == {"backendNodeId": 42}


class TestClickFallback:
    async def test_runtime_call_failure_falls_back_to_bounds(self) -> None:
        # bounds.center == (450, 520). After Layer 1, those are CSS pixels.
        node = _make_node(42, DOMRect(x=400.0, y=500.0, width=100.0, height=40.0))
        cdp = FakeCDP(raise_on={"Runtime.callFunctionOn"})
        wd = _watchdog(cdp, {42: node})

        result = await wd._on_click_element(ClickElementEvent(backend_node_id=42))
        assert _outcome(result) == {"clicked": True, "x": 450, "y": 520}

        mouse_events = [c for c in cdp.calls if c.method == "Input.dispatchMouseEvent"]
        assert len(mouse_events) == 3
        for evt in mouse_events:
            assert (evt.params["x"], evt.params["y"]) == (450, 520)

    async def test_a_snapshot_centre_outside_the_viewport_is_refused(self) -> None:
        """No live box, and the snapshot's centre (450, 820) is below a 720-high viewport."""
        node = _make_node(42, DOMRect(x=400.0, y=800.0, width=100.0, height=40.0))
        cdp = FakeCDP(raise_on={"Runtime.callFunctionOn"})
        wd = _watchdog(cdp, {42: node})

        result = await wd._on_click_element(ClickElementEvent(backend_node_id=42))
        assert result is not None and "outside the viewport" in result["error"]
        assert [c for c in cdp.calls if c.method == "Input.dispatchMouseEvent"] == []

    async def test_resolve_node_failure_falls_back_to_bounds(self) -> None:
        node = _make_node(42, DOMRect(x=10.0, y=20.0, width=4.0, height=8.0))
        cdp = FakeCDP(raise_on={"DOM.resolveNode"})
        wd = _watchdog(cdp, {42: node})

        result = await wd._on_click_element(ClickElementEvent(backend_node_id=42))
        # bounds.center == (12, 24).
        assert _outcome(result) == {"clicked": True, "x": 12, "y": 24}

    async def test_scroll_into_view_failure_does_not_abort(self) -> None:
        """scrollIntoViewIfNeeded failing is logged but doesn't block the click."""
        node = _make_node(42, DOMRect(x=400.0, y=800.0, width=100.0, height=40.0))
        cdp = FakeCDP(
            rect={"x": 50.0, "y": 60.0, "w": 10.0, "h": 20.0},
            raise_on={"DOM.scrollIntoViewIfNeeded"},
        )
        wd = _watchdog(cdp, {42: node})

        result = await wd._on_click_element(ClickElementEvent(backend_node_id=42))
        # Live rect path still succeeds → live coords (55, 70).
        assert _outcome(result) == {"clicked": True, "x": 55, "y": 70}

    async def test_a_live_centre_outside_the_viewport_is_centred_then_refused(self) -> None:
        """The element is centred by script once; still outside, nothing is pressed."""
        node = _make_node(42, DOMRect(x=1700.0, y=40.0, width=120.0, height=40.0))
        cdp = FakeCDP(rect={"x": 1700.0, "y": 40.0, "w": 120.0, "h": 40.0})
        wd = _watchdog(cdp, {42: node})

        result = await wd._on_click_element(ClickElementEvent(backend_node_id=42))
        assert result is not None and "outside the viewport" in result["error"]
        centring = [
            c
            for c in cdp.calls
            if c.method == "Runtime.callFunctionOn"
            and "scrollIntoView" in c.params.get("functionDeclaration", "")
        ]
        assert len(centring) == 1
        assert [c for c in cdp.calls if c.method == "Input.dispatchMouseEvent"] == []

    async def test_unknown_node_returns_error(self) -> None:
        cdp = FakeCDP()
        wd = _watchdog(cdp, {})

        result = await wd._on_click_element(ClickElementEvent(backend_node_id=999))
        assert result is not None
        assert "999" in (result.get("error") or "")
        assert cdp.calls == []
