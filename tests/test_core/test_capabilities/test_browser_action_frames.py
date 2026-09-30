"""Acting on an element inside frames.

Three rules, checked here on the call log of a recording CDP:

* every command *about the node* goes to the session that owns the node —
  the out-of-process frame's session for an element in a cross-origin
  iframe, the tab's for everything in the tab's own target;
* mouse input always goes to the tab's session, at the page point: a frame
  element's layout box (``DOM.getContentQuads``, in its local root's
  viewport) lifted at each process boundary by the owner's content box —
  and no script runs in the element's frame to measure it;
* a frame element that can no longer be measured is not clicked at its
  snapshot position, and the action says so.

The selector map comes from the real cleaner run on
``tests._helpers.dom_payloads.reference_page``, so the nodes carry real parent
links, sessions and namespaced keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.click import ClickAction
from spectragent.core.capabilities.browser.actions.extract_content import ExtractContentAction
from spectragent.core.capabilities.browser.actions.scroll import ScrollAction
from spectragent.core.capabilities.browser.actions.select_dropdown import SelectDropdownAction
from spectragent.core.capabilities.browser.actions.type_action import TypeAction
from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMSelectorMap
from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import (
    ClickElementEvent,
    ExtractContentEvent,
    ScrollEvent,
    SelectDropdownEvent,
    TypeTextEvent,
)
from tests._helpers.dom_payloads import clean, reference_page
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
    FakeElementNode,
    FakeScope,
)


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    session_id: str | None


Box = tuple[float, float, float, float]


def quad(x: float, y: float, w: float, h: float) -> list[float]:
    return [x, y, x + w, y, x + w, y + h, x, y + h]


def box_model(
    content: list[float], border: list[float], width: float, height: float
) -> dict[str, Any]:
    """A ``DOM.getBoxModel`` answer: two quads plus the *layout* border size.

    ``width`` / ``height`` are the border box's size before any transform —
    which is what lets the projection recover the frame's own viewport size
    from a content quad a transform has stretched.
    """
    return {"content": list(content), "border": list(border), "width": width, "height": height}


def plain_frame(x: float, y: float) -> dict[str, Any]:
    """An untransformed frame owner: 100x100 content box at ``(x, y)``."""
    return box_model(quad(x, y, 100, 100), quad(x - 1, y - 1, 102, 102), 102, 102)


class FrameCDP:
    """Answers geometry per ``(session, backend id)`` the way Chrome does.

    ``quads`` are ``DOM.getContentQuads`` answers, in the node's local root's
    viewport (the page for the tab's own target). ``owner_content`` is where a
    frame owner's content box starts, in *its* local root; ``owner_models``
    overrides it with a whole ``DOM.getBoxModel`` answer, which is how a frame
    under a CSS transform is described — its content quad is a parallelogram.
    ``rects`` are live ``getBoundingClientRect`` results for top-level
    elements, whose path is script.
    """

    def __init__(
        self,
        *,
        quads: dict[tuple[str, int], list[Box]] | None = None,
        raw_quads: dict[tuple[str, int], list[list[float]]] | None = None,
        owner_content: dict[tuple[str, int], tuple[float, float]] | None = None,
        owner_models: dict[tuple[str, int], dict[str, Any]] | None = None,
        rects: dict[tuple[str, int], Box] | None = None,
        raise_on: set[tuple[str, str | None]] | None = None,
    ) -> None:
        self.calls: list[Call] = []
        self.quads = quads if quads is not None else dict(QUADS)
        #: Quads that are not axis-aligned — an element inside a frame the
        #: page draws under a CSS transform, which Blink already composed.
        self.raw_quads = raw_quads or {}
        self.owner_content = owner_content if owner_content is not None else dict(OWNER_CONTENT)
        self.owner_models = owner_models or {}
        self.rects = rects if rects is not None else dict(RECTS)
        self.raise_on = raise_on or set()

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> Any:
        params = params or {}
        self.calls.append(Call(method, params, session_id))
        if (method, session_id) in self.raise_on:
            raise RuntimeError(f"CDP error: injected failure: {method} on {session_id}")
        if method == "Browser.getVersion":
            return {"userAgent": "Chrome (X11; Linux x86_64)"}
        key = (session_id or "", params.get("backendNodeId", -1))
        if method == "DOM.getContentQuads":
            if key in self.raw_quads:
                return {"quads": [list(q) for q in self.raw_quads[key]]}
            if key not in self.quads:
                raise RuntimeError("CDP error: Could not compute content quads.")
            return {"quads": [quad(*box) for box in self.quads[key]]}
        if method == "DOM.getBoxModel":
            if key in self.owner_models:
                return {"model": self.owner_models[key]}
            if key not in self.owner_content:
                raise RuntimeError("CDP error: Could not compute box model.")
            return {"model": plain_frame(*self.owner_content[key])}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"{session_id}|{params['backendNodeId']}"}}
        if method == "Runtime.callFunctionOn" and params.get("returnByValue"):
            session, backend_id = params["objectId"].split("|")
            x, y, w, h = self.rects[(session, int(backend_id))]
            return {"result": {"value": {"x": x, "y": y, "w": w, "h": h}}}
        return {}

    def mouse(self) -> list[Call]:
        return [c for c in self.calls if c.method == "Input.dispatchMouseEvent"]

    def about_nodes(self) -> list[Call]:
        return [c for c in self.calls if not c.method.startswith("Input.")]


# Live geometry the fake browser reports for the reference page.
QUADS: dict[tuple[str, int], list[Box]] = {
    ("cross", 10): [(0.0, 40.0, 30.0, 30.0)],  # #cb, in the cross frame's viewport
    ("cross", 11): [(0.0, 72.0, 150.0, 21.0)],  # #cross-input
    ("nested", 10): [(5.0, 7.0, 60.0, 21.0)],  # #nested-btn, in the nested frame's viewport
    ("tab", 52): [(112.0, 274.0, 50.0, 20.0)],  # #same-btn: same process, page viewport
}
OWNER_CONTENT: dict[tuple[str, int], tuple[float, float]] = {
    ("tab", 15): (50.0, 510.0),  # #cross, in the page
    ("cross", 12): (5.0, 115.0),  # #nested, in the cross frame
    ("tab", 11): (112.0, 212.0),  # #same, in the page (never needed: same process)
}
RECTS: dict[tuple[str, int], Box] = {
    ("tab", 10): (0.0, 100.0, 80.0, 20.0),  # #main-btn
    ("tab", 14): (0.0, 402.0, 60.0, 20.0),  # #shadow-btn
}


class _Scope:
    def connect(self, *_a: Any, **_k: Any) -> None:
        return None


def watchdog(cdp: FrameCDP, selector_map: DOMSelectorMap, **kwargs: Any) -> ActionWatchdog:
    wd = ActionWatchdog(cdp, selector_map_getter=lambda: selector_map, **kwargs)  # type: ignore[arg-type]
    wd.attach(_Scope(), session_id="tab")  # type: ignore[arg-type]
    return wd


class Straight:
    """A humanized strategy that goes straight to the endpoint and records it."""

    humanized = True

    def __init__(self) -> None:
        self.seen: list[tuple[float, float, float, float]] = []

    def click_hold_ms(self) -> float:
        return 0.0

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        """Dead centre: these tests are about where the *box* is.

        The landing-point scatter is the strategy's own (see
        ``test_click_landing.py``), so a strategy that declines it leaves the
        composed point exact and these assertions at full resolution.
        """
        return (0.0, 0.0)

    def generate(
        self,
        sx: float,
        sy: float,
        ex: float,
        ey: float,
        *,
        target_w: float = 0.0,
        target_h: float = 0.0,
    ) -> list[TrajectoryPoint]:
        self.seen.append((ex, ey, target_w, target_h))
        return [TrajectoryPoint(round(ex), round(ey), 0.0)]


@pytest.fixture
async def selector_map() -> DOMSelectorMap:
    return (await clean(reference_page())).selector_map


def node(selector_map: DOMSelectorMap, element_id: str) -> DOMElementNode:
    for candidate in selector_map.values():
        if candidate.attributes.get("id") == element_id:
            return candidate
    raise AssertionError(element_id)


class TestClick:
    async def test_out_of_process_element(self, selector_map: DOMSelectorMap) -> None:
        cdp = FrameCDP()
        cb = node(selector_map, "cb")
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="cross")
        )

        # (0, 40, 30x30) in the frame + #cross's content origin (50, 510) → centre.
        assert result == {"clicked": True, "x": 65, "y": 565}
        assert [(c.method, c.session_id) for c in cdp.about_nodes()] == [
            ("DOM.scrollIntoViewIfNeeded", "cross"),
            ("DOM.getContentQuads", "cross"),
            ("DOM.getBoxModel", "tab"),
        ]
        assert cdp.about_nodes()[2].params == {
            "backendNodeId": cb.frame_owners()[0].backend_node_id
        }
        assert {(c.params["x"], c.params["y"], c.session_id) for c in cdp.mouse()} == {
            (65, 565, "tab")
        }

    async def test_nested_frames_add_every_process_boundary(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP()
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="nested")
        )
        # (5, 7, 60x21) + #nested (5, 115) in cross + #cross (50, 510) in page.
        assert result == {"clicked": True, "x": 90, "y": 642}
        owner_boxes = [(c.session_id, c.params) for c in cdp.calls if c.method == "DOM.getBoxModel"]
        assert owner_boxes == [
            ("cross", {"backendNodeId": 12}),
            ("tab", {"backendNodeId": 15}),
        ]
        assert {c.session_id for c in cdp.mouse()} == {"tab"}

    async def test_a_same_process_frame_element_is_already_in_page_coordinates(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP()
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=52, session_id="tab")
        )
        assert result == {"clicked": True, "x": 137, "y": 284}
        assert [c.method for c in cdp.calls] == [
            "DOM.scrollIntoViewIfNeeded",
            "DOM.getContentQuads",
            "Input.dispatchMouseEvent",
            "Input.dispatchMouseEvent",
            "Input.dispatchMouseEvent",
        ]

    async def test_a_shadow_root_element_takes_the_page_path(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP()
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=14, session_id="tab")
        )
        assert result == {"clicked": True, "x": 30, "y": 412}
        assert [c.method for c in cdp.calls] == [
            "DOM.scrollIntoViewIfNeeded",
            "DOM.resolveNode",
            "Runtime.callFunctionOn",
            "Input.dispatchMouseEvent",
            "Input.dispatchMouseEvent",
            "Input.dispatchMouseEvent",
        ]

    @pytest.mark.parametrize(("session", "backend_id"), [("cross", 10), ("nested", 10)])
    async def test_no_script_runs_to_measure_a_frame_element(
        self, selector_map: DOMSelectorMap, session: str, backend_id: int
    ) -> None:
        cdp = FrameCDP()
        await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=backend_id, session_id=session)
        )
        assert not [
            c for c in cdp.calls if c.method.startswith("Runtime.") or c.method == "DOM.resolveNode"
        ]

    async def test_the_quads_of_a_wrapped_element_are_boxed_together(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP(
            quads={**QUADS, ("cross", 10): [(0.0, 40.0, 30.0, 10.0), (0.0, 50.0, 20.0, 20.0)]}
        )
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="cross")
        )
        assert result == {"clicked": True, "x": 65, "y": 565}

    async def test_without_quads_the_border_box_is_used(self, selector_map: DOMSelectorMap) -> None:
        quads = dict(QUADS)
        del quads[("cross", 10)]
        cdp = FrameCDP(quads=quads, owner_content={**OWNER_CONTENT, ("cross", 10): (1.0, 41.0)})
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="cross")
        )
        # border box (0, 40, 102x102) in the frame + (50, 510).
        assert result == {"clicked": True, "x": 101, "y": 601}

    async def test_the_same_backend_id_in_two_targets_is_two_elements(
        self, selector_map: DOMSelectorMap
    ) -> None:
        wd = watchdog(FrameCDP(), selector_map)
        page_button = wd._resolve_node(10, None)
        frame_checkbox = wd._resolve_node(10, "cross")
        assert page_button is not None and page_button.attributes["id"] == "main-btn"
        assert frame_checkbox is not None and frame_checkbox.attributes["id"] == "cb"
        assert wd._resolve_node(10, "tab") is page_button
        assert wd._resolve_node(10, "gone") is None

    async def test_the_trajectory_gets_the_page_point_and_the_element_box(
        self, selector_map: DOMSelectorMap
    ) -> None:
        strategy = Straight()
        cdp = FrameCDP()
        await watchdog(cdp, selector_map, mouse_trajectory=strategy)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="cross")
        )
        assert strategy.seen == [(65, 565, 30, 30)]


def transformed_frame(
    origin: tuple[float, float],
    ex: tuple[float, float],
    ey: tuple[float, float],
    *,
    content: tuple[float, float] = (320.0, 180.0),
    border: tuple[float, float] = (340.0, 200.0),
) -> dict[str, Any]:
    """A frame owner whose transform sends one CSS pixel to ``ex`` / ``ey``.

    ``content`` / ``border`` are the frame's *layout* sizes; the quads are
    what the transform makes of them, and ``width`` / ``height`` stay the
    layout border box — which is how Chrome answers.
    """

    def corners(width: float, height: float) -> list[float]:
        return [
            value
            for x, y in ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height))
            for value in (
                origin[0] + x * ex[0] + y * ey[0],
                origin[1] + x * ex[1] + y * ey[1],
            )
        ]

    return box_model(corners(*content), corners(*border), border[0], border[1])


class TestFramesUnderATransform:
    """A frame's contents land where it is drawn, not where it would be at 1:1.

    Every owner here is a *cross-origin* frame, because that is where the
    composition happens: Blink stops at the process boundary and the owner's
    box model is the only thing that describes it. The same-origin cases at
    the end are the control — Chrome has already composed those.
    """

    async def test_a_scaled_frame_scales_the_point(self, selector_map: DOMSelectorMap) -> None:
        cdp = FrameCDP(
            quads={**QUADS, ("cross", 10): [(200.0, 100.0, 40.0, 20.0)]},
            owner_models={("tab", 15): transformed_frame((100.0, 200.0), (0.5, 0.0), (0.0, 0.5))},
        )
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="cross")
        )
        # (100, 200) + 0.5 * (220, 110), the element's centre in the frame.
        # Adding the content origin alone would have said (320, 310).
        assert result == {"clicked": True, "x": 210, "y": 255}

    async def test_a_rotated_frame_turns_the_element_with_it(
        self, selector_map: DOMSelectorMap
    ) -> None:
        strategy = Straight()
        cdp = FrameCDP(
            quads={**QUADS, ("cross", 10): [(200.0, 100.0, 40.0, 20.0)]},
            # rotate(90deg) scale(.5): the frame's x axis runs down the page.
            owner_models={("tab", 15): transformed_frame((500.0, 100.0), (0.0, 0.5), (-0.5, 0.0))},
        )
        result = await watchdog(cdp, selector_map, mouse_trajectory=strategy)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="cross")
        )
        assert result == {"clicked": True, "x": 445, "y": 210}
        # The target the trajectory aims at turns too: 40x20 becomes 20x40.
        assert strategy.seen == [(445, 210, 10, 20)]

    async def test_every_boundary_of_a_nested_frame_is_mapped(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP(
            quads={**QUADS, ("nested", 10): [(8.0, 8.0, 64.0, 24.0)]},
            owner_models={
                ("cross", 12): transformed_frame((10.0, 100.0), (0.5, 0.0), (0.0, 0.5)),
                ("tab", 15): transformed_frame((100.0, 200.0), (0.5, 0.0), (0.0, 0.5)),
            },
        )
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="nested")
        )
        # (8, 8, 64x24) → halved into #nested at (10, 100) → halved again
        # into #cross at (100, 200): a quarter-size box at (107, 252).
        assert result == {"clicked": True, "x": 115, "y": 255}

    async def test_a_frame_under_perspective_is_refused_by_name(
        self, selector_map: DOMSelectorMap
    ) -> None:
        model = transformed_frame((100.0, 200.0), (1.0, 0.0), (0.0, 1.0))
        model["content"][4] += 60.0  # one corner moves: a perspective quad
        cdp = FrameCDP(owner_models={("tab", 15): model})

        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="cross")
        )
        assert result is not None and "perspective transform" in result["error"]
        assert cdp.mouse() == []

    async def test_a_frame_scaled_to_nothing_is_refused(self, selector_map: DOMSelectorMap) -> None:
        cdp = FrameCDP(
            owner_models={("tab", 15): transformed_frame((100.0, 200.0), (0.0, 0.0), (0.0, 0.0))}
        )
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="cross")
        )
        assert result is not None and "observe the page again" in result["error"]
        assert cdp.mouse() == []

    async def test_a_same_origin_frame_needs_no_projection(
        self, selector_map: DOMSelectorMap
    ) -> None:
        """Blink composed the transform already: the quad arrives page-shaped."""
        cdp = FrameCDP(
            raw_quads={("tab", 52): [[100.0, 260.0, 160.0, 270.0, 150.0, 300.0, 90.0, 290.0]]}
        )
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=52, session_id="tab")
        )
        assert result == {"clicked": True, "x": 125, "y": 280}
        assert [c.method for c in cdp.about_nodes()] == [
            "DOM.scrollIntoViewIfNeeded",
            "DOM.getContentQuads",
        ]

    async def test_a_page_element_is_untouched_by_any_of_it(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP()
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="tab")
        )
        assert result == {"clicked": True, "x": 40, "y": 110}

    async def test_typing_still_reaches_a_frame_it_cannot_aim_at(
        self, selector_map: DOMSelectorMap
    ) -> None:
        """Keystrokes go through the element's own session; only the cursor cannot."""
        model = transformed_frame((100.0, 200.0), (1.0, 0.0), (0.0, 1.0))
        model["content"][4] += 60.0
        cdp = FrameCDP(owner_models={("tab", 15): model})

        result = await watchdog(cdp, selector_map, mouse_trajectory=Straight())._on_type_text(
            TypeTextEvent(backend_node_id=11, session_id="cross", text="hi")
        )
        assert result == {"typed": True, "length": 2}
        assert cdp.mouse() == []

    async def test_scrolling_an_unmappable_frame_element_does_nothing(
        self, selector_map: DOMSelectorMap
    ) -> None:
        model = transformed_frame((100.0, 200.0), (1.0, 0.0), (0.0, 1.0))
        model["content"][4] += 60.0
        cdp = FrameCDP(owner_models={("tab", 15): model})

        await watchdog(cdp, selector_map)._on_scroll(
            ScrollEvent(direction="down", amount=120, backend_node_id=10, session_id="cross")
        )
        assert cdp.mouse() == []


class TestAFrameThatChangedIsNotClickedBlind:
    """The snapshot's point belongs to whatever replaced the element."""

    @pytest.mark.parametrize(
        "failure",
        [
            # The frame's renderer is gone: a cross-site navigation or removal.
            {("DOM.getContentQuads", "cross"), ("DOM.getBoxModel", "cross")},
            # The frame is there, its owner cannot be measured.
            {("DOM.getBoxModel", "tab")},
        ],
        ids=["element", "owner"],
    )
    async def test_a_frame_element_that_cannot_be_measured_is_refused(
        self, selector_map: DOMSelectorMap, failure: set[tuple[str, str | None]]
    ) -> None:
        cdp = FrameCDP(raise_on=failure)
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="cross")
        )
        assert result is not None and "observe the page again" in result["error"]
        assert cdp.mouse() == []

    async def test_a_same_process_frame_element_too(self, selector_map: DOMSelectorMap) -> None:
        quads = dict(QUADS)
        del quads[("tab", 52)]
        owner_content = dict(OWNER_CONTENT)
        cdp = FrameCDP(quads=quads, owner_content=owner_content)
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=52, session_id="tab")
        )
        assert result is not None and "error" in result
        assert cdp.mouse() == []

    async def test_a_page_element_keeps_its_snapshot_fallback(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP(raise_on={("Runtime.callFunctionOn", "tab")})
        result = await watchdog(cdp, selector_map)._on_click_element(
            ClickElementEvent(backend_node_id=10, session_id="tab")
        )
        # #main-btn's snapshot bounds (0, 200, 80x20).
        assert result == {"clicked": True, "x": 40, "y": 210}

    async def test_nor_scrolled_at_blind(self, selector_map: DOMSelectorMap) -> None:
        cdp = FrameCDP(raise_on={("DOM.getBoxModel", "tab")})
        await watchdog(cdp, selector_map)._on_scroll(
            ScrollEvent(direction="down", amount=120, backend_node_id=10, session_id="cross")
        )
        assert cdp.mouse() == []


class TestKeyboardAndDom:
    async def test_type_focuses_and_types_through_the_frame_session(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP()
        result = await watchdog(cdp, selector_map)._on_type_text(
            TypeTextEvent(backend_node_id=11, session_id="cross", text="hi")
        )
        assert result == {"typed": True, "length": 2}
        keyboard = [
            (c.method, c.session_id) for c in cdp.calls if c.method != "Input.dispatchMouseEvent"
        ]
        assert keyboard == [
            ("DOM.focus", "cross"),
            ("Browser.getVersion", None),
            ("Input.dispatchKeyEvent", "cross"),
            ("Input.dispatchKeyEvent", "cross"),
            ("Input.dispatchKeyEvent", "cross"),
            ("Input.dispatchKeyEvent", "cross"),
            ("Input.dispatchKeyEvent", "cross"),
            ("Input.dispatchKeyEvent", "cross"),
            ("Input.insertText", "cross"),
        ]

    async def test_humanized_type_approaches_the_page_point_first(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP()
        await watchdog(cdp, selector_map, mouse_trajectory=Straight())._on_type_text(
            TypeTextEvent(backend_node_id=11, session_id="cross", text="hi")
        )
        (move,) = cdp.mouse()
        # (0, 72, 150x21) in the frame + #cross (50, 510).
        assert (move.params["x"], move.params["y"], move.session_id) == (125, 592, "tab")

    async def test_select_runs_in_the_frame_session(self, selector_map: DOMSelectorMap) -> None:
        cdp = FrameCDP()
        await watchdog(cdp, selector_map)._on_select_dropdown(
            SelectDropdownEvent(backend_node_id=11, session_id="cross", value="b")
        )
        assert {c.session_id for c in cdp.calls} == {"cross"}
        assert cdp.calls[-1].params["arguments"] == [{"value": "b"}]

    async def test_extract_reads_the_element_in_its_frame_and_the_page_in_the_tab(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP()
        wd = watchdog(cdp, selector_map)
        await wd._on_extract_content(ExtractContentEvent(backend_node_id=11, session_id="cross"))
        await wd._on_extract_content(ExtractContentEvent())
        assert [(c.method, c.session_id) for c in cdp.calls] == [
            ("DOM.getOuterHTML", "cross"),
            ("Runtime.evaluate", "tab"),
        ]

    async def test_scroll_wheels_the_tab_at_the_page_point(
        self, selector_map: DOMSelectorMap
    ) -> None:
        cdp = FrameCDP()
        await watchdog(cdp, selector_map)._on_scroll(
            ScrollEvent(direction="down", amount=120, backend_node_id=10, session_id="cross")
        )
        (wheel,) = cdp.mouse()
        assert (wheel.params["x"], wheel.params["y"], wheel.params["deltaY"], wheel.session_id) == (
            65,
            565,
            120,
            "tab",
        )


class _AnsweringScope(FakeScope):
    """Records each emitted event and answers it the way one handler would."""

    def __init__(self, answer: dict[str, Any] | None) -> None:
        super().__init__()
        self.answer = answer

    def emit(self, event: Any) -> None:
        super().emit(event)
        event.record_result(connection_id="c1", handler_name="ActionWatchdog", result=self.answer)


def _capability(scope: FakeScope) -> FakeCapability:
    cap = FakeCapability()
    cap._scopes["tab"] = scope
    cap._selector_maps["tab"] = {
        2**31 + 10: FakeElementNode(backend_node_id=10, session_id="cross")
    }
    cap._default_session_id = "tab"
    return cap


class TestActionsAddressTheOwningSession:
    @pytest.mark.parametrize(
        ("action", "params", "event_type"),
        [
            (ClickAction(post_delay_ms=0), {}, ClickElementEvent),
            (TypeAction(post_delay_ms=0), {"text": "x"}, TypeTextEvent),
            (SelectDropdownAction(post_delay_ms=0), {"value": "b"}, SelectDropdownEvent),
            (ScrollAction(post_delay_ms=0), {"direction": "down"}, ScrollEvent),
            (ExtractContentAction(), {}, ExtractContentEvent),
        ],
    )
    async def test_event_carries_backend_id_and_session(
        self, action: Any, params: dict[str, Any], event_type: type
    ) -> None:
        capability = _capability(FakeScope())
        result = await action.execute({"index": 2**31 + 10, **params}, capability=capability)
        assert result.success, result.error
        (event,) = capability._scopes["tab"].emitted
        assert isinstance(event, event_type)
        assert (event.backend_node_id, event.session_id) == (10, "cross")


class TestActionsReportWhatTheHandlerDid:
    @pytest.mark.parametrize(
        ("action", "params"),
        [(ClickAction(post_delay_ms=0), {}), (TypeAction(post_delay_ms=0), {"text": "x"})],
        ids=["click", "type"],
    )
    async def test_a_refused_action_is_a_failed_action(
        self, action: Any, params: dict[str, Any]
    ) -> None:
        scope = _AnsweringScope({"error": "The element is inside a frame that changed"})
        result = await action.execute(
            {"index": 2**31 + 10, **params}, capability=_capability(scope)
        )
        assert not result.success
        assert result.error == "The element is inside a frame that changed"

    @pytest.mark.parametrize(
        ("action", "params", "answer"),
        [
            (ClickAction(post_delay_ms=0), {}, {"clicked": True, "x": 1, "y": 2}),
            (TypeAction(post_delay_ms=0), {"text": "x"}, {"typed": True, "length": 1}),
        ],
        ids=["click", "type"],
    )
    async def test_a_performed_action_succeeds(
        self, action: Any, params: dict[str, Any], answer: dict[str, Any]
    ) -> None:
        result = await action.execute(
            {"index": 2**31 + 10, **params}, capability=_capability(_AnsweringScope(answer))
        )
        assert result.success, result.error
