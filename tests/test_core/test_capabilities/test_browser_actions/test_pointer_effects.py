"""What a click or a drag measurably did: ``PointerEffect``.

``docs/architecture_design.md`` §4.5, "动作效果": the pointer actions report
where they pressed, what the page hit-tested there right before the press,
whether that is the element a click by index named, and the focused element
afterwards. Measured by the executor around its own dispatch; a probe that
fails or runs out of ``action_effect_timeout_s`` leaves its fact unknown and
never changes the action's outcome; a failed action reports no effect.

Every test runs the real action against a real ``ActionWatchdog`` on a
scripted CDP (``_effect_tab.py``). The real-Chrome counterpart is
``test_browser_effects_pointer_live.py``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.click import ClickAction
from spectragent.core.capabilities.browser.actions.click_coordinate import ClickCoordinateAction
from spectragent.core.capabilities.browser.actions.drag import DragAction
from spectragent.core.capabilities.browser.effects import ElementFacts, PointerEffect, ViewportPoint
from spectragent.schemas.action import ActionResult
from tests.test_core.test_capabilities.test_browser_actions._effect_tab import (
    CENTRE,
    ProbeCDP,
    effect_tab,
    facts,
    frame_node,
    page_node,
)

GO = facts("button", "Go")
NAME = facts("textbox", "Name")
BANNER = facts("generic", "Cookie banner")


async def click(cdp: ProbeCDP, **watchdog_kwargs: Any) -> ActionResult:
    """``browser.click`` on index 7, a page element whose live centre is :data:`CENTRE`."""
    async with effect_tab(cdp, nodes={7: page_node()}, **watchdog_kwargs) as tab:
        return await ClickAction(post_delay_ms=0).execute({"index": 7}, capability=tab)  # type: ignore[arg-type]


async def click_at(cdp: ProbeCDP, x: int = 5, y: int = 5, **watchdog_kwargs: Any) -> ActionResult:
    async with effect_tab(cdp, **watchdog_kwargs) as tab:
        return await ClickCoordinateAction(post_delay_ms=0).execute(
            {"coordinate_x": x, "coordinate_y": y},
            capability=tab,  # type: ignore[arg-type]
        )


async def drag(cdp: ProbeCDP, **watchdog_kwargs: Any) -> ActionResult:
    async with effect_tab(cdp, **watchdog_kwargs) as tab:
        return await DragAction(post_delay_ms=0).execute(
            {"start_x": 10, "start_y": 20, "end_x": 30, "end_y": 40},
            capability=tab,  # type: ignore[arg-type]
        )


def pointer(result: ActionResult) -> PointerEffect:
    assert result.success, result.error
    assert isinstance(result.effect, PointerEffect)
    return result.effect


class TestDeclared:
    @pytest.mark.parametrize("action", [ClickAction, ClickCoordinateAction, DragAction])
    def test_the_pointer_actions_share_the_pointer_effect(self, action: type) -> None:
        assert action.Effect is PointerEffect


class TestClickByIndex:
    async def test_a_hit_on_the_target(self) -> None:
        cdp = ProbeCDP(hit={"facts": GO, "on_target": True}, focus={"facts": NAME})
        effect = pointer(await click(cdp))
        assert effect.effect_id == "browser.click"
        assert effect.point == ViewportPoint(x=CENTRE[0], y=CENTRE[1])
        assert effect.release_point is None
        assert effect.hit == ElementFacts(role="button", name="Go")
        assert effect.on_target is True
        assert effect.focus_after == ElementFacts(role="textbox", name="Name")
        assert effect.describe() == (
            'Clicked at (55, 70) on button "Go", the targeted element; '
            'focus is now on textbox "Name".'
        )

    async def test_an_occluded_target(self) -> None:
        cdp = ProbeCDP(hit={"facts": BANNER, "on_target": False})
        effect = pointer(await click(cdp))
        assert (effect.hit, effect.on_target, effect.focus_after) == (
            ElementFacts(role="generic", name="Cookie banner"),
            False,
            "nothing",
        )
        assert effect.describe() == (
            'Clicked at (55, 70) on generic "Cookie banner", not the targeted element; '
            "nothing is focused now."
        )

    async def test_the_hit_test_is_on_the_target_object_and_runs_between_arrival_and_press(
        self,
    ) -> None:
        cdp = ProbeCDP(hit={"facts": GO, "on_target": True})
        await click(cdp)
        hit = cdp.calls[cdp.index("hit")]
        assert hit.method == "Runtime.callFunctionOn"
        assert hit.params["objectId"] == "target"
        assert hit.params["arguments"] == [{"value": CENTRE[0]}, {"value": CENTRE[1]}]
        assert cdp.index("mouseMoved") < cdp.index("hit") < cdp.index("mousePressed")
        assert cdp.index("mouseReleased") < cdp.index("focus")
        # The target object the probe held is released once the click is measured.
        release = cdp.calls[cdp.index("release")]
        assert release.params == {"objectId": "target"}
        assert cdp.index("focus") < cdp.index("release")

    async def test_a_target_that_cannot_be_resolved_is_unknown_and_the_hit_still_measured(
        self,
    ) -> None:
        # Every resolve fails: the click falls back to the snapshot box
        # (centre 450, 520) and the hit test asks the point alone.
        cdp = ProbeCDP(hit={"facts": GO}, fail={"resolve"})
        result = await click(cdp)
        effect = pointer(result)
        assert effect.point == ViewportPoint(x=450, y=520)
        assert (effect.hit, effect.on_target) == (ElementFacts(role="button", name="Go"), "unknown")
        assert effect.describe() == (
            'Clicked at (450, 520) on button "Go"; whether that is the targeted element is '
            "unknown; nothing is focused now."
        )

    async def test_an_element_inside_a_frame_is_not_resolved(self) -> None:
        cdp = ProbeCDP(hit={"facts": facts("frame")})
        async with effect_tab(cdp, nodes={7: frame_node()}) as tab:
            result = await ClickAction(post_delay_ms=0).execute({"index": 7}, capability=tab)  # type: ignore[arg-type]
        effect = pointer(result)
        assert effect.on_target == "unavailable"
        assert effect.hit == ElementFacts(role="frame")
        assert "resolve" not in cdp.kinds()
        assert effect.describe() == (
            "Clicked at (55, 70) on frame; whether that is the targeted element is not "
            "measurable here; nothing is focused now."
        )


class TestClickCoordinate:
    async def test_names_no_target(self) -> None:
        cdp = ProbeCDP(hit={"nothing": True}, focus={"nothing": True})
        effect = pointer(await click_at(cdp, 5, 5))
        assert effect.effect_id == "browser.click_coordinate"
        assert (effect.point, effect.hit, effect.on_target, effect.focus_after) == (
            ViewportPoint(x=5, y=5),
            "nothing",
            None,
            "nothing",
        )
        assert effect.describe() == "Clicked at (5, 5) on no element; nothing is focused now."
        hit = cdp.calls[cdp.index("hit")]
        assert hit.method == "Runtime.evaluate" and "deepHit(5, 5)" in hit.params["expression"]
        assert hit.session_id == "tab"
        assert "resolve" not in cdp.kinds()

    async def test_focus_in_a_frame_of_another_site_is_unavailable(self) -> None:
        cdp = ProbeCDP(hit={"facts": facts("frame", "Payment")}, focus={"opaque": True})
        effect = pointer(await click_at(cdp, 300, 200))
        assert effect.focus_after == "unavailable"
        assert effect.describe() == (
            'Clicked at (300, 200) on frame "Payment"; what is focused now is not measurable here.'
        )


class TestMeasuringNeverDecidesTheOutcome:
    async def test_a_failing_hit_test_is_unknown(self) -> None:
        cdp = ProbeCDP(fail={"hit"}, focus={"facts": NAME})
        effect = pointer(await click(cdp))
        assert (effect.hit, effect.on_target) == ("unknown", "unknown")
        assert effect.focus_after == ElementFacts(role="textbox", name="Name")
        assert effect.describe() == (
            "Clicked at (55, 70) on an unknown element; whether that is the targeted element "
            'is unknown; focus is now on textbox "Name".'
        )

    async def test_a_hit_test_that_never_answers_spends_the_budget_and_no_more(self) -> None:
        cdp = ProbeCDP(hang={"hit"})
        started = time.monotonic()
        effect = pointer(await click_at(cdp, action_effect_timeout_s=0.05))
        assert time.monotonic() - started < 1.0
        assert (effect.hit, effect.focus_after) == ("unknown", "unknown")
        # The budget is spent: the focus is not even asked.
        assert "focus" not in cdp.kinds()
        assert [c.params["type"] for c in cdp.inputs()] == [
            "mouseMoved",
            "mousePressed",
            "mouseReleased",
        ]

    async def test_a_click_that_failed_reports_no_effect(self) -> None:
        cdp = ProbeCDP(fail_methods={"Input.dispatchMouseEvent"})
        result = await click(cdp)
        assert not result.success
        assert result.effect is None
        # The target object is let go all the same.
        assert "release" in cdp.kinds()

    async def test_a_coordinate_click_that_failed_reports_no_effect(self) -> None:
        result = await click_at(ProbeCDP(fail_methods={"Input.dispatchMouseEvent"}))
        assert (result.success, result.effect) == (False, None)


class TestDrag:
    async def test_press_and_release_points_and_what_was_pressed(self) -> None:
        cdp = ProbeCDP(hit={"facts": facts("slider", "Volume")})
        effect = pointer(await drag(cdp))
        assert effect.effect_id == "browser.drag"
        assert (effect.point, effect.release_point) == (
            ViewportPoint(x=10, y=20),
            ViewportPoint(x=30, y=40),
        )
        assert (effect.hit, effect.on_target, effect.focus_after) == (
            ElementFacts(role="slider", name="Volume"),
            None,
            "nothing",
        )
        assert effect.describe() == (
            'Dragged from (10, 20) to (30, 40), pressing on slider "Volume"; '
            "nothing is focused now."
        )

    async def test_the_hit_test_is_at_the_press_point_before_the_button_goes_down(self) -> None:
        cdp = ProbeCDP()
        await drag(cdp)
        assert "deepHit(10, 20)" in cdp.calls[cdp.index("hit")].params["expression"]
        assert cdp.index("mouseMoved") < cdp.index("hit") < cdp.index("mousePressed")
        assert cdp.index("mouseReleased") < cdp.index("focus")

    async def test_a_drag_that_failed_reports_no_effect(self) -> None:
        result = await drag(ProbeCDP(fail_methods={"Input.dispatchMouseEvent"}))
        assert (result.success, result.effect) == (False, None)

    async def test_a_failing_probe_leaves_the_drag_alone(self) -> None:
        effect = pointer(await drag(ProbeCDP(fail={"hit", "focus"})))
        assert (effect.hit, effect.focus_after) == ("unknown", "unknown")
        assert effect.describe() == (
            "Dragged from (10, 20) to (30, 40), pressing on an unknown element; "
            "what is focused now is unknown."
        )
