"""What ``browser.scroll`` reports it moved: the container, its offsets, its edge.

A wheel scrolls whatever the page routes it to from the point it is
dispatched at, so the scroll's effect is measured, not assumed: the probe holds
the containers that could take the wheel, reads their offsets before it and
after it lands, and reports the innermost one that moved — or, when nothing
moved, the one the wheel was aimed at, with a measured zero
(``docs/architecture_design.md`` §4.5, "动作效果"). Measuring never fails the
scroll: a probe that fails or runs out of budget leaves facts unknown.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.scroll import ScrollAction
from spectragent.core.capabilities.browser.effect_probes import ProbeBudget
from spectragent.core.capabilities.browser.effects import ElementFacts, ScrollEffect, ScrollOffset
from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint
from spectragent.core.capabilities.browser.scroll_probe import ScrollProbe
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import ScrollEvent
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    answering_capability,
)


def _item(y: int, max_y: int, *, page: bool, name: str = "", x: int = 0) -> dict[str, Any]:
    return {
        "x": x,
        "y": y,
        "max_x": 0,
        "max_y": max_y,
        "page": page,
        "connected": True,
        "facts": None if page else {"role": "generic", "name": name},
    }


class ScriptedPage:
    """A fake CDP whose scroll containers report a scripted sequence of offsets."""

    def __init__(self, reads: list[list[dict[str, Any]]], *, frame: bool = False) -> None:
        self._reads: Iterator[list[dict[str, Any]]] = iter(reads)
        self._last: list[dict[str, Any]] = reads[-1]
        self.frame = frame
        self.methods: list[str] = []
        self.fail: set[str] = set()

    async def send(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        self.methods.append(method)
        if method in self.fail:
            raise RuntimeError(f"{method} failed")
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "objectId": "chain-1"}}
        if method == "Runtime.callFunctionOn":
            items = next(self._reads, self._last)
            return {"result": {"value": {"frame": self.frame, "items": items}}}
        return {}


async def _measure(
    page: ScriptedPage, direction: str = "down", budget: float = 1.0
) -> ScrollEffect:
    probe = ScrollProbe(page, "sid", ProbeBudget(budget), direction=direction)  # type: ignore[arg-type]
    await probe.before(10, 10)
    facts = await probe.after()
    await probe.release()
    return ScrollEffect(effect_id="browser.scroll", **facts)


async def test_the_page_moved() -> None:
    page = ScriptedPage(
        [[_item(0, 2280, page=True)], [_item(300, 2280, page=True)], [_item(300, 2280, page=True)]]
    )
    effect = await _measure(page)
    assert effect.container == "page" and effect.element is None
    assert effect.before == ScrollOffset(x=0, y=0) and effect.after == ScrollOffset(x=0, y=300)
    assert effect.delta == ScrollOffset(x=0, y=300)
    assert effect.at_boundary is False
    assert effect.describe() == (
        "Scroll down moved the page by 300 px (y 0 → 300); it can scroll further down."
    )
    assert "Runtime.releaseObject" in page.methods


async def test_a_wheel_that_lands_late_is_waited_for() -> None:
    """The first wheel on a fresh page was measured landing ~0.9 s after dispatch."""
    still = [_item(0, 2280, page=True)]
    page = ScriptedPage([still, still, still, still, [_item(300, 2280, page=True)]])
    effect = await _measure(page)
    assert effect.delta == ScrollOffset(x=0, y=300)


async def test_the_inner_box_moved_and_is_named() -> None:
    page = ScriptedPage(
        [
            [_item(0, 1000, page=False, name="Results"), _item(0, 2280, page=True)],
            [_item(1000, 1000, page=False, name="Results"), _item(0, 2280, page=True)],
        ]
    )
    effect = await _measure(page)
    assert effect.container == "element"
    assert effect.element == ElementFacts(role="generic", name="Results")
    assert effect.at_boundary is True
    assert effect.describe() == (
        'Scroll down moved element generic "Results" by 1000 px (y 0 → 1000); '
        "it is at its bottom edge."
    )


async def test_nothing_moved_at_the_edge_is_a_measured_zero_without_waiting() -> None:
    page = ScriptedPage([[_item(2280, 2280, page=True)]])
    effect = await _measure(page)
    # Every held container was at its edge: no move could come, so zero is measured.
    assert effect.delta == ScrollOffset(x=0, y=0)
    assert effect.at_boundary is True
    # One read before, one after, one to confirm: no polling for a move that cannot come.
    assert page.methods.count("Runtime.callFunctionOn") == 3
    assert effect.describe() == (
        "Scroll down moved the page by 0 px (y 2280 → 2280); it is at its bottom edge."
    )


async def test_a_page_that_cannot_scroll_reports_the_page_at_its_edge() -> None:
    page = ScriptedPage([[_item(0, 0, page=True)]])
    effect = await _measure(page, direction="up")
    assert effect.container == "page"
    assert effect.delta == ScrollOffset(x=0, y=0)
    assert effect.at_boundary is True


async def test_nothing_moved_over_another_sites_frame_is_unavailable() -> None:
    page = ScriptedPage([[_item(0, 2280, page=True)]], frame=True)
    effect = await _measure(page, budget=0.2)
    assert effect.container == "unavailable"
    assert effect.before == effect.after == effect.at_boundary == "unavailable"
    assert effect.delta is None


async def test_a_failed_probe_leaves_every_fact_unknown() -> None:
    page = ScriptedPage([[_item(0, 2280, page=True)]])
    page.fail.add("Runtime.evaluate")
    effect = await _measure(page)
    assert effect.container == effect.before == effect.after == effect.at_boundary == "unknown"
    assert effect.describe() == (
        "Scroll down went to a container that is unknown; how far it moved is unknown; "
        "whether it is at its bottom edge is unknown."
    )


async def test_an_exhausted_budget_sends_nothing_more() -> None:
    page = ScriptedPage([[_item(0, 2280, page=True)]])
    effect = await _measure(page, budget=1e-9)
    assert effect.container == "unknown"


class _Mouse:
    """Stands in for ``HumanMouse``: records the wheel, moves the scripted page."""

    def __init__(self, page: ScriptedPage) -> None:
        self.page = page
        self.wheels: list[tuple[int, int, int, int]] = []
        self.position = (45, 34)

    async def wheel(self, x: int, y: int, dx: int, dy: int) -> None:
        self.wheels.append((x, y, dx, dy))


async def test_the_executor_measures_around_the_wheel_at_the_pointer() -> None:
    page = ScriptedPage(
        [[_item(0, 2280, page=True)], [_item(300, 2280, page=True)], [_item(300, 2280, page=True)]]
    )
    watchdog = ActionWatchdog(page, input_platform="linux", selector_map_getter=dict)  # type: ignore[arg-type]
    mouse = _Mouse(page)
    watchdog._mouse = mouse  # type: ignore[assignment]
    answer = await watchdog._on_scroll(ScrollEvent(direction="down", amount=300))
    assert mouse.wheels == [(45, 34, 0, 300)]
    assert answer["scrolled"] is True
    effect = ScrollEffect(effect_id="browser.scroll", **answer["effect_facts"])
    assert effect.delta == ScrollOffset(x=0, y=300)


class _RecordingPage(ScriptedPage):
    """A scripted page that also keeps every mouse event and every evaluated expression."""

    def __init__(self, reads: list[list[dict[str, Any]]]) -> None:
        super().__init__(reads)
        self.mouse: list[tuple[str, int, int]] = []
        self.evaluated: list[str] = []

    async def send(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        if method == "Input.dispatchMouseEvent" and params is not None:
            self.mouse.append((params["type"], params["x"], params["y"]))
        if method == "Runtime.evaluate" and params is not None:
            self.evaluated.append(params["expression"])
        return await super().send(method, params, **kw)


class _StraightPath:
    """Humanized, one waypoint per move; keeps every aim offset it is asked for."""

    humanized = True

    def __init__(self) -> None:
        self.aims: list[tuple[float, float]] = []

    def generate(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        target_w: float = 0.0,
        target_h: float = 0.0,
    ) -> list[TrajectoryPoint]:
        return [TrajectoryPoint(round(end_x), round(end_y), 0.0)]

    def click_hold_ms(self) -> float:
        return 0.0

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        self.aims.append((width, height))
        return 0.0, 0.0


def _box_scrolled() -> _RecordingPage:
    """A box under the start point that the wheel moves by 300 px."""
    box = [_item(0, 1000, page=False, name="Results"), _item(0, 2280, page=True)]
    moved = [_item(300, 1000, page=False, name="Results"), _item(0, 2280, page=True)]
    return _RecordingPage([box, moved, moved])


async def test_a_started_scroll_moves_the_pointer_to_its_start_then_wheels_there() -> None:
    """Under ``instant`` too: the pointer goes to the start, so the next observation reports it there."""
    page = _box_scrolled()
    watchdog = ActionWatchdog(page, input_platform="linux", selector_map_getter=dict)  # type: ignore[arg-type]
    answer = await watchdog._on_scroll(
        ScrollEvent(direction="down", amount=300, start_x=300, start_y=200)
    )
    assert page.mouse == [("mouseMoved", 300, 200), ("mouseWheel", 300, 200)]
    assert watchdog._mouse.observed_position == (300, 200)
    # Measured where the wheel turned: the containers under the start point.
    assert page.evaluated[0].rstrip().endswith("(300, 200)")
    effect = ScrollEffect(effect_id="browser.scroll", **answer["effect_facts"])
    assert effect.container == "element" and effect.delta == ScrollOffset(x=0, y=300)


async def test_a_humanized_start_is_reached_along_the_path_and_never_scattered() -> None:
    """The start is the model's own point, as a coordinate click's is: no aim offset."""
    page, path = _box_scrolled(), _StraightPath()
    watchdog = ActionWatchdog(
        page,  # type: ignore[arg-type]
        input_platform="linux",
        selector_map_getter=dict,
        mouse_trajectory=path,  # type: ignore[arg-type]
    )
    await watchdog._on_scroll(ScrollEvent(direction="down", amount=300, start_x=300, start_y=200))
    assert path.aims == []
    moves = [(x, y) for kind, x, y in page.mouse if kind == "mouseMoved"]
    wheels = {(x, y) for kind, x, y in page.mouse if kind == "mouseWheel"}
    assert moves[-1] == (300, 200) and wheels == {(300, 200)}


@pytest.mark.parametrize("facts", [None, {"direction": "down"}])
async def test_the_action_reports_its_outcome_whatever_the_measurement(facts: Any) -> None:
    """No facts, or facts the model refuses: the scroll still succeeded, with no effect."""
    capability = answering_capability({"scrolled": True, "effect_facts": facts})
    result = await ScrollAction().execute({"direction": "down"}, capability=capability)  # type: ignore[arg-type]
    assert result.success is True
    assert result.effect is None


async def test_the_action_carries_the_measured_effect() -> None:
    facts = {
        "direction": "down",
        "container": "page",
        "before": ScrollOffset(x=0, y=0),
        "after": ScrollOffset(x=0, y=300),
        "at_boundary": False,
    }
    capability = answering_capability({"scrolled": True, "effect_facts": facts})
    result = await ScrollAction().execute({"direction": "down"}, capability=capability)  # type: ignore[arg-type]
    assert isinstance(result.effect, ScrollEffect)
    assert result.effect.effect_id == "browser.scroll"
    assert result.effect.delta == ScrollOffset(x=0, y=300)


async def test_a_wheel_that_never_lands_costs_no_more_than_the_budget() -> None:
    """Room to move but nothing moves (a late wheel, or a page that cancels wheel events).

    The wait is bounded, and what happened after it is unknown, never a measured
    zero: the wheel may land after the budget.
    """
    import time

    page = ScriptedPage([[_item(0, 2280, page=True)]])
    started = time.monotonic()
    effect = await _measure(page, budget=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 0.35, f"the measurement took {elapsed:.3f} s on a 0.2 s budget"
    assert effect.container == "page"
    assert effect.before == ScrollOffset(x=0, y=0)
    assert effect.after == effect.at_boundary == "unknown"
    assert effect.delta is None
    assert effect.describe() == (
        "Scroll down went to the page; how far it moved is unknown; "
        "whether it is at its bottom edge is unknown."
    )


async def test_a_still_box_with_room_is_unknown_even_when_the_page_is_at_its_edge() -> None:
    """Only every held container being at its edge makes nothing-moved a measured zero."""
    page = ScriptedPage(
        [[_item(0, 1000, page=False, name="Results"), _item(2280, 2280, page=True)]]
    )
    effect = await _measure(page, budget=0.2)
    assert effect.container == "element"
    assert effect.element == ElementFacts(role="generic", name="Results")
    assert effect.after == effect.at_boundary == "unknown"
