"""Changed click targets refuse input without turning unknown probes into changes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.click import ClickAction
from spectragent.core.capabilities.browser.actions.click_coordinate import ClickCoordinateAction
from spectragent.core.capabilities.browser.actions.type_coordinate import TypeCoordinateAction
from spectragent.core.capabilities.browser.click_target import ClickTargetGuard
from spectragent.core.capabilities.browser.effect_probes import ProbeBudget
from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from tests.test_core.test_capabilities.test_browser_actions._effect_tab import (
    Call,
    ProbeCDP,
    effect_tab,
    page_node,
)


class TargetCDP(ProbeCDP):
    """Only target identity responses are scripted; action dispatch stays real."""

    is_connected = True

    def on_event(self, method: str, callback: Any) -> None:
        return None

    def off_event(self, method: str, callback: Any) -> None:
        return None

    def __init__(self, *, changed: bool | str = True, missing: bool = False, **kwargs: Any):
        super().__init__(**kwargs)
        self.changed = changed
        self.missing = missing
        self.target_fail: str | None = None
        self.target_hang: str | None = None

    async def send(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        source = params.get("expression", params.get("functionDeclaration", ""))
        kind = next(
            (
                stage
                for stage in ("snapshot", "check")
                if f"spectragent click target {stage}" in source
            ),
            None,
        )
        if kind is None:
            return await super().send(method, params, session_id, timeout)
        self.calls.append(Call(method, params, session_id))
        if self.target_fail == kind:
            raise RuntimeError("target identity inaccessible")
        if self.target_hang == kind:
            await asyncio.Event().wait()
        if kind == "snapshot":
            if self.missing:
                return {"result": {"type": "object", "subtype": "null", "value": None}}
            return {"result": {"type": "object", "objectId": "click-target"}}
        return {"result": {"value": self.changed}}

    def target_calls(self, stage: str) -> list[Call]:
        return [
            call
            for call in self.calls
            if f"spectragent click target {stage}"
            in call.params.get("expression", call.params.get("functionDeclaration", ""))
        ]


@pytest.mark.parametrize("changed", [False, True, "unknown"])
async def test_target_identity_verdict_and_reference_lifetime(changed):
    cdp = TargetCDP(changed=changed)
    guard = ClickTargetGuard(cdp, "tab", ProbeBudget(0.1))
    await guard.capture(300, 200)
    assert await guard.changed(300, 200) == changed
    assert len(cdp.target_calls("snapshot")) == len(cdp.target_calls("check")) == 1
    check = cdp.target_calls("check")[0]
    assert check.params["objectId"] == "click-target"
    assert check.session_id == "tab"
    await guard.close()
    await guard.close()
    releases = [call for call in cdp.calls if call.method == "Runtime.releaseObjectGroup"]
    assert len(releases) == 1
    assert releases[0].params == {
        "objectGroup": cdp.target_calls("snapshot")[0].params["objectGroup"]
    }


async def test_no_target_identity_is_unknown_without_a_check():
    cdp = TargetCDP(missing=True)
    guard = ClickTargetGuard(cdp, "tab", ProbeBudget(0.1))
    await guard.capture(300, 200)
    assert await guard.changed(300, 200) == "unknown"
    await guard.close()
    assert not cdp.target_calls("check")
    assert len([call for call in cdp.calls if call.method == "Runtime.releaseObjectGroup"]) == 1


@pytest.mark.parametrize("stage", ["snapshot", "check"])
@pytest.mark.parametrize("problem", ["target_fail", "target_hang"])
async def test_unavailable_or_timed_out_identity_is_unknown(stage, problem):
    cdp = TargetCDP()
    setattr(cdp, problem, stage)
    guard = ClickTargetGuard(cdp, "tab", ProbeBudget(0.01))
    await asyncio.wait_for(guard.capture(300, 200), 0.5)
    assert await asyncio.wait_for(guard.changed(300, 200), 0.5) == "unknown"
    await guard.close()
    releases = [call for call in cdp.calls if call.method == "Runtime.releaseObjectGroup"]
    assert len(releases) == 1


@pytest.mark.parametrize("kind", ["index", "coordinate", "type_coordinate"])
async def test_changed_target_refuses_press_release_and_following_text_input(kind):
    cdp = TargetCDP()
    async with effect_tab(cdp, nodes={7: page_node()}) as tab:
        if kind == "index":
            result = await ClickAction(post_delay_ms=0).execute({"index": 7}, capability=tab)  # type: ignore[arg-type]
        elif kind == "coordinate":
            result = await ClickCoordinateAction(post_delay_ms=0).execute(
                {"coordinate_x": 55, "coordinate_y": 70},
                capability=tab,  # type: ignore[arg-type]
            )
        else:
            result = await TypeCoordinateAction(post_delay_ms=0).execute(
                {"coordinate_x": 55, "coordinate_y": 70, "text": "must not enter", "clear": True},
                capability=tab,  # type: ignore[arg-type]
            )
    assert not result.success and result.effect is None
    assert result.error and "changed" in result.error.lower()
    assert "observ" in result.error.lower()
    assert [call.params.get("type") for call in cdp.inputs()] == ["mouseMoved"]
    snapshot = cdp.calls.index(cdp.target_calls("snapshot")[0])
    checked = cdp.calls.index(cdp.target_calls("check")[0])
    assert snapshot < cdp.index("mouseMoved") < checked
    assert any(
        call.method == "Runtime.releaseObjectGroup"
        and call.params["objectGroup"] == cdp.target_calls("snapshot")[0].params["objectGroup"]
        for call in cdp.calls
    )


@pytest.mark.parametrize("changed", [False, "unknown"])
async def test_stable_or_unknown_target_preserves_the_requested_click(changed):
    cdp = TargetCDP(changed=changed)
    async with effect_tab(cdp) as tab:
        result = await ClickCoordinateAction(post_delay_ms=0).execute(
            {"coordinate_x": 55, "coordinate_y": 70},
            capability=tab,  # type: ignore[arg-type]
        )
    assert result.success, result.error
    assert [call.params["type"] for call in cdp.inputs()] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert all((call.params["x"], call.params["y"]) == (55, 70) for call in cdp.inputs())


async def test_spent_diagnostic_budget_does_not_disable_target_guard(monkeypatch):
    monkeypatch.setattr(ActionWatchdog, "_effect_budget", lambda self: ProbeBudget(0))
    cdp = TargetCDP()
    async with effect_tab(cdp) as tab:
        result = await ClickCoordinateAction(post_delay_ms=0).execute(
            {"coordinate_x": 55, "coordinate_y": 70},
            capability=tab,  # type: ignore[arg-type]
        )
    assert not result.success
    assert len(cdp.target_calls("check")) == 1
    assert [call.params["type"] for call in cdp.inputs()] == ["mouseMoved"]


class SlowPath:
    humanized = True

    def generate(self, sx, sy, ex, ey, **kwargs):
        return [TrajectoryPoint(round(sx), round(sy)), TrajectoryPoint(round(ex), round(ey), 80)]

    def click_hold_ms(self):
        return 0.0

    def aim_offset_px(self, width, height):
        return 0.0, 0.0


async def test_trajectory_time_does_not_spend_target_check_budget():
    cdp = TargetCDP()
    async with effect_tab(cdp, mouse_trajectory=SlowPath(), action_effect_timeout_s=0.02) as tab:
        result = await ClickCoordinateAction(post_delay_ms=0).execute(
            {"coordinate_x": 55, "coordinate_y": 70},
            capability=tab,  # type: ignore[arg-type]
        )
    assert not result.success
    assert len(cdp.target_calls("check")) == 1
    assert all(call.params["type"] == "mouseMoved" for call in cdp.inputs())


@pytest.mark.parametrize("stage", ["snapshot", "check"])
async def test_cancellation_releases_target_group_even_without_a_snapshot_reply(stage):
    entered = asyncio.Event()

    class BlockedTargetCDP(TargetCDP):
        async def send(self, method, params=None, session_id=None, timeout=None):
            params = params or {}
            source = params.get("expression", params.get("functionDeclaration", ""))
            if f"spectragent click target {stage}" in source:
                self.calls.append(Call(method, params, session_id))
                entered.set()
                await asyncio.Future()
            return await super().send(method, params, session_id, timeout)

    cdp = BlockedTargetCDP()
    watchdog = ActionWatchdog(cdp, input_platform="linux")
    task = asyncio.create_task(watchdog._click_at(55, 70, "left", 1))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    releases = [call for call in cdp.calls if call.method == "Runtime.releaseObjectGroup"]
    assert len(releases) == 1
    assert (
        releases[0].params["objectGroup"] == cdp.target_calls("snapshot")[0].params["objectGroup"]
    )
    assert all(call.params["type"] == "mouseMoved" for call in cdp.inputs())


async def test_repeated_cancellation_preserves_running_reference_cleanup():
    releasing = asyncio.Event()
    release_allowed = asyncio.Event()

    class HeldCleanupCDP(TargetCDP):
        async def send(self, method, params=None, session_id=None, timeout=None):
            if method == "Runtime.releaseObjectGroup":
                releasing.set()
                await release_allowed.wait()
            return await super().send(method, params, session_id, timeout)

    cdp = HeldCleanupCDP()
    watchdog = ActionWatchdog(cdp, input_platform="linux")
    task = asyncio.create_task(watchdog._click_at(55, 70, "left", 1))
    await asyncio.wait_for(releasing.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert len([call for call in cdp.calls if call.method == "Runtime.releaseObjectGroup"]) == 1
