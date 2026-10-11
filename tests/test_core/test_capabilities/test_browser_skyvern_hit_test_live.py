"""``skyvern_hit_test`` in a real Chrome, on the fixture site's ``sticky`` page (M32, arch §4.12).

A sticky header over links the page scrolled underneath it — the M18 defect:
the ``skyvern`` cleaner indexes the link the header covers, and an agent
clicking ``[5]`` clicks the header. The variant asks the page itself.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMState, HitTest
from spectragent.core.composer import ConfigComposer
from spectragent.schemas.action import Action
from spectragent.scripts.generate_all import _collect_registrars
from tests._helpers.fixture_site import FixtureSite

pytestmark = pytest.mark.requires_chrome


@pytest.fixture
def site() -> Iterator[FixtureSite]:
    with FixtureSite() as fixture:
        yield fixture


def _capability(cleaner: dict[str, Any]) -> BrowserCapability:
    run = ConfigComposer(registrars=_collect_registrars()).compose(
        {
            "capability": [
                {
                    "capability_id": "browser",
                    "profile": "skyvern",
                    "mouse_trajectory": {"trajectory_type": "instant"},
                    "typing_rhythm": {"rhythm_type": "instant"},
                    "replayability": {"replayability_id": "off"},
                    "network_source": {"source_id": "live"},
                    "dom_cleaner": cleaner,
                    "dom_serializer": {"serializer_type": "text"},
                    "backend": {"provider_id": "local_chrome", "headless": True},
                }
            ]
        }
    )
    cap = run.single("capability")
    assert isinstance(cap, BrowserCapability)
    return cap


def _nodes(state: DOMState) -> dict[str, DOMElementNode]:
    assert state.dom_tree is not None
    return {
        node.attributes["id"]: node
        for node in [state.dom_tree, *state.dom_tree.descendants()]
        if "id" in node.attributes
    }


def _indexed(state: DOMState) -> set[str]:
    return {node.attributes.get("id", "") for node in state.selector_map.values()}


async def _observe(cap: BrowserCapability) -> tuple[DOMState, float]:
    cap._cycle_cache.pop(cap.default_session_id, None)
    started = time.perf_counter()
    fragment = await cap.resolve_fragment("browser.dom")
    elapsed = time.perf_counter() - started
    assert isinstance(fragment.element_tree, DOMState)
    return fragment.element_tree, elapsed


async def _hash(cap: BrowserCapability) -> str:
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": "location.hash", "returnByValue": True},
        session_id=cap.default_session_id,
    )
    return str(reply["result"]["value"])


async def test_skyvern_indexes_the_covered_link(site: FixtureSite) -> None:
    """The defect the variant exists for, reproduced."""
    cap = _capability({"cleaner_type": "skyvern"})
    try:
        await cap.setup(start_url=site.url("sticky"))
        state, _ = await _observe(cap)
        assert "covered" in _indexed(state)
        assert all(node.hit_test is None for node in state.selector_map.values())
    finally:
        await cap.teardown()


async def test_drop_hides_the_covered_link_and_the_points_are_clickable(
    site: FixtureSite,
) -> None:
    cap = _capability({"cleaner_type": "skyvern_hit_test"})
    try:
        await cap.setup(start_url=site.url("sticky"))
        state, _ = await _observe(cap)
        nodes = _nodes(state)
        assert "covered" not in _indexed(state)
        assert nodes["covered"].element_index is None
        assert nodes["covered"].hit_test == HitTest(center_topmost=False, visible_point=None)

        # the centre of "partial" is under the header; its visible point is below it
        partial = nodes["partial"].hit_test
        assert partial is not None and not partial.center_topmost
        assert partial.visible_point is not None
        x, y = partial.visible_point
        assert 300 <= x < 540 and 100 <= y < 140, partial
        assert nodes["clear"].hit_test == HitTest(True, (700.0, 220.0))
        # a checkbox its own label's text covers is still reached through the label
        agree = nodes["agree"].hit_test
        assert agree is not None and agree.center_topmost
        # off-screen is not covered: no hit test, still indexed
        assert nodes["below"].hit_test is None and "below" in _indexed(state)
        # an element of a same-process iframe, placed in the top-level viewport
        inside = nodes["inside"].hit_test
        assert inside is not None and inside.center_topmost and inside.visible_point is not None
        assert 860 <= inside.visible_point[0] < 1160 and 100 <= inside.visible_point[1] < 220

        # the recorded point is one a click reaches
        result = await cap.execute_action(
            Action(
                name="browser.click_coordinate",
                params={"coordinate_x": round(x), "coordinate_y": round(y)},
            )
        )
        assert result.success, result.error
        assert await _hash(cap) == "#partial"
    finally:
        await cap.teardown()


async def test_keep_indexes_it_with_the_hit_test(site: FixtureSite) -> None:
    cap = _capability({"cleaner_type": "skyvern_hit_test", "covered_elements": "keep"})
    try:
        await cap.setup(start_url=site.url("sticky"))
        state, _ = await _observe(cap)
        covered = next(
            n for n in state.selector_map.values() if n.attributes.get("id") == "covered"
        )
        assert covered.hit_test == HitTest(center_topmost=False, visible_point=None)
    finally:
        await cap.teardown()


async def test_what_the_hit_test_costs_per_observation(site: FixtureSite) -> None:
    """Measured, not asserted tight: printed for the record (arch §4.12 cites it)."""
    timings: dict[str, float] = {}
    for cleaner in ("skyvern", "skyvern_hit_test"):
        cap = _capability({"cleaner_type": cleaner})
        try:
            await cap.setup(start_url=site.url("sticky"))
            await _observe(cap)  # warm
            samples = [(await _observe(cap))[1] for _ in range(5)]
            timings[cleaner] = sorted(samples)[len(samples) // 2]
        finally:
            await cap.teardown()
    print(
        f"median DOM observation: skyvern {timings['skyvern'] * 1000:.1f} ms, "
        f"skyvern_hit_test {timings['skyvern_hit_test'] * 1000:.1f} ms"
    )
    assert timings["skyvern_hit_test"] < timings["skyvern"] + 2.0
