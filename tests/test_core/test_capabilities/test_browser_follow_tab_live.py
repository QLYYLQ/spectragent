"""Following a tab the agent's actions opened, in a real Chrome (§4.14 "页面新开的标签页").

Against the local fixture site only (``tests/_helpers/fixture_site.py``,
scenario ``new_tab``; no network, no model spend):

- a click on a ``target=_blank`` link (no ``rel``, ``rel=noopener``) or a
  button calling ``window.open`` (with and without ``noopener``) makes the
  next observation show the new tab, and every fragment of that capture
  records the move (``focus_moved``);
- ``new_tabs: stay`` leaves the focus where it was, the new tab listed;
- a followed tab that closes itself hands the focus back to the tab that
  opened it;
- a tab a page opens on its own — from a timer while nobody acts, from a
  timer during an action, or as it loads after the agent navigated — is
  listed and never followed (Chrome reports no user activation for the last
  two);
- ``browser.switch_tab`` still moves the focus, either way;
- a recorded run that followed a new tab replays offline along the same
  path, and a branch run restores it — the prefix re-executed, the focus
  move reproduced, every restored step verified — through the real runtime.

The pure rules are in ``test_browser_tabs.py``; the capability on the fake
browser in ``test_browser_follow_tab.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from spectragent.core.benchmark_runner import TaskOutcome
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
from spectragent.core.capabilities.browser.tabs import FocusMove
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionResult
from tests._helpers import branch_driver as branch
from tests._helpers import replay_driver as driver
from tests._helpers.archive_bench import FIXTURE_SETTLE_QUIET_MS
from tests._helpers.fixture_site import (
    NEW_TAB_CLOSES_AFTER_MS,
    NEW_TAB_TIMER_MS,
    FixtureSite,
    new_tab_point,
)
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 90
FRAGMENTS = frozenset({"browser.browser_meta", "browser.screenshot"})


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with FixtureSite() as fixture:
        yield fixture


@asynccontextmanager
async def _browser(
    site: FixtureSite, *, new_tabs: str = "follow"
) -> AsyncIterator[BrowserCapability]:
    cap = BrowserCapability(settle_quiet_ms=FIXTURE_SETTLE_QUIET_MS, new_tabs=new_tabs)  # type: ignore[arg-type]
    cap.backend = LocalChromeProvider(headless=True, extra_args=site.chrome_args())  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(negotiation=set(FRAGMENTS)), 60)
        yield cap
    finally:
        await cap.teardown()


class _Look:
    """One observation cycle, the way ``Environment.observe`` drives it."""

    def __init__(self, cap: BrowserCapability) -> None:
        self.cap = cap
        self.cycle = 0

    async def __call__(self) -> tuple[BrowserMetaObs, ScreenshotObs]:
        self.cycle += 1
        await self.cap.begin_observation(self.cycle)
        meta = await asyncio.wait_for(self.cap.resolve_fragment("browser.browser_meta"), 30)
        shot = await asyncio.wait_for(self.cap.resolve_fragment("browser.screenshot"), 30)
        assert isinstance(meta, BrowserMetaObs) and isinstance(shot, ScreenshotObs)
        assert meta.focus_moved == shot.focus_moved, "one record for every fragment of a capture"
        return meta, shot


async def _act(cap: BrowserCapability, name: str, **params: Any) -> ActionResult:
    result = await asyncio.wait_for(cap.execute_action(Action(name=name, params=params)), 60)
    assert result.success, f"{name}{params}: {result.error}"
    return result


async def _click(cap: BrowserCapability, control: str) -> None:
    x, y = new_tab_point(control)
    await _act(cap, "browser.click_coordinate", coordinate_x=x, coordinate_y=y)


def _focused(cap: BrowserCapability) -> str:
    return cap._session_to_target[cap.default_session_id]


def _active(meta: BrowserMetaObs) -> list[str]:
    return [tab["tab_id"] for tab in meta.tabs if tab["is_active"]]


# ── follow ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("how", ["blank", "blank_noopener", "window_open", "window_open_noopener"])
async def test_the_tab_a_click_opened_is_observed_next(site: FixtureSite, how: str) -> None:
    async def run() -> None:
        async with _browser(site) as cap:
            look = _Look(cap)
            await _act(cap, "browser.navigate", url=site.url("new_tab"))
            _, before = await look()
            opener = _focused(cap)
            await _click(cap, how)
            meta, shot = await look()
            followed = _focused(cap)
            assert followed != opener
            assert f"how={how}" in meta.url, meta.url
            assert meta.focus_moved == FocusMove(reason="new_tab", from_tab=opener, to_tab=followed)
            assert _active(meta) == [followed]
            assert {tab["tab_id"] for tab in meta.tabs} == {opener, followed}
            assert shot.image_bytes != before.image_bytes, "the screenshot is of the new page"
            assert shot.settle is not None, "the observation waited on the tab it captured"
            # Nothing moves on the next observation: the window closed with the capture.
            await _act(cap, "browser.wait", seconds=0.2)
            meta, _ = await look()
            assert meta.focus_moved is None and _focused(cap) == followed

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_stay_leaves_the_focus_on_the_tab_the_agent_acted_on(site: FixtureSite) -> None:
    async def run() -> None:
        async with _browser(site, new_tabs="stay") as cap:
            look = _Look(cap)
            await _act(cap, "browser.navigate", url=site.url("new_tab"))
            await look()
            opener = _focused(cap)
            await _click(cap, "blank")
            meta, _ = await look()
            assert _focused(cap) == opener and meta.url == site.url("new_tab")
            assert meta.focus_moved is None
            assert len(meta.tabs) == 2, "the new tab is listed, reachable with switch_tab"

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_a_followed_tab_that_closes_itself_hands_the_focus_back(site: FixtureSite) -> None:
    async def run() -> None:
        async with _browser(site) as cap:
            look = _Look(cap)
            await _act(cap, "browser.navigate", url=site.url("new_tab"))
            await look()
            opener = _focused(cap)
            await _click(cap, "closes")
            meta, _ = await look()
            followed = _focused(cap)
            assert "how=closes" in meta.url and followed != opener
            # The agent waits on the followed tab; the tab closes itself meanwhile.
            await _act(cap, "browser.wait", seconds=NEW_TAB_CLOSES_AFTER_MS / 1000 + 1.5)
            meta, _ = await look()
            assert _focused(cap) == opener
            assert meta.url == site.url("new_tab")
            assert meta.focus_moved == FocusMove(
                reason="tab_closed", from_tab=followed, to_tab=opener
            )
            assert followed not in cap._target_to_session, "what it held for the tab is released"
            assert [tab["tab_id"] for tab in meta.tabs] == [opener]

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_a_tab_a_page_opens_on_its_own_is_not_followed(site: FixtureSite) -> None:
    timer_url = f"{site.main_origin}/new_tab/timer.html"

    async def run() -> None:
        async with _browser(site) as cap:
            look = _Look(cap)
            # A timer while nobody acts: outside every follow window.
            await _act(cap, "browser.navigate", url=timer_url)
            meta, _ = await look()
            page = _focused(cap)
            assert meta.url == timer_url and len(meta.tabs) == 1, "observed before the timer fired"
            await asyncio.sleep(NEW_TAB_TIMER_MS / 1000 + 1.0)
            meta, _ = await look()
            assert _focused(cap) == page and meta.focus_moved is None
            assert any("how=timer" in tab["url"] for tab in meta.tabs), "listed, not followed"
            # A timer during an action: Chrome reports no user activation for it.
            await _act(cap, "browser.navigate", url=timer_url)
            await _act(cap, "browser.wait", seconds=NEW_TAB_TIMER_MS / 1000 + 1.0)
            meta, _ = await look()
            assert _focused(cap) == page and meta.focus_moved is None
            assert sum("how=timer" in tab["url"] for tab in meta.tabs) == 2
            # A popup a page opens as it loads, after the agent navigated to it.
            await _act(cap, "browser.navigate", url=site.url("popup"))
            meta, _ = await look()
            assert _focused(cap) == page and meta.url == site.url("popup")
            assert any(tab["url"].endswith("/popup/popup.html") for tab in meta.tabs)

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_switch_tab_still_moves_the_focus(site: FixtureSite) -> None:
    async def run() -> None:
        async with _browser(site) as cap:
            look = _Look(cap)
            await _act(cap, "browser.navigate", url=site.url("new_tab"))
            await look()
            opener = _focused(cap)
            await _click(cap, "blank_noopener")
            await look()
            followed = _focused(cap)
            assert followed != opener
            await _act(cap, "browser.switch_tab", target_id=opener)
            meta, _ = await look()
            assert _focused(cap) == opener and meta.url == site.url("new_tab")
            assert meta.focus_moved is None, "the agent's own switch is its action, not a move"
            await _act(cap, "browser.switch_tab", target_id=None)  # the most recently opened
            meta, _ = await look()
            assert _focused(cap) == followed and "how=blank_noopener" in meta.url

    await asyncio.wait_for(run(), _TIMEOUT_S)


# ── recorded, replayed, branched ──────────────────────────────────────

TASK = "Open the link in its new tab, look at it, and finish."
#: Step 0 clicks a target=_blank link; step 1 observes the tab the focus followed and clicks
#: in it; step 2 finishes there. The branch restores 0..1 and decides step 2 again.
RECORDING = [
    driver.click_at(*new_tab_point("replay")),
    driver.click_at(640, 600),
    driver.done("followed"),
]
CHECKPOINT = 2


@dataclass
class Flow:
    source: driver.Outcome
    replay: driver.Outcome
    branch_outcome: TaskOutcome
    branch: RunTrace


@pytest.fixture(scope="module")
def mapped_site() -> Iterator[FixtureSite]:
    with pytest.MonkeyPatch.context() as patch:
        # The scripted models are on loopback; the machine's proxy must not see them.
        patch.setenv("NO_PROXY", "127.0.0.1,localhost")
        patch.setenv("no_proxy", "127.0.0.1,localhost")
        with FixtureSite(mapped=True) as fixture:
            yield fixture


@pytest.fixture(scope="module")
def flow(mapped_site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Flow:
    base = tmp_path_factory.mktemp("follow_tab")
    start = mapped_site.url("new_tab")
    with ScriptedLLMServer(RECORDING) as llm:
        raw = driver.record_config(site=mapped_site, llm=llm, trace_dir=base / "recorded")
        source = asyncio.run(driver.run_task(raw, task=TASK, start_url=start))
    assert llm.errors == [], llm.errors
    source.require_finished(len(RECORDING), "followed")

    with ScriptedLLMServer(driver.recorded_completions(source.trace)) as llm:
        replay_raw = driver.to_replay_config(raw, llm=llm, trace_dir=base / "replay")
        replay = asyncio.run(
            driver.run_task(replay_raw, task=TASK, start_url=start, replay_archive=source.run_dir)
        )
    assert llm.errors == [], llm.errors
    replay.require_finished(len(RECORDING), "followed")

    with ScriptedLLMServer([driver.done("followed")]) as teacher:
        config = branch.branch_config(
            raw,
            llm=teacher,
            trace_dir=base / "branches",
            sources_root=base / "recorded",
            step=CHECKPOINT,
            continue_steps=0,
        )
        outcome = branch.run_branch_task(config, f"{source.run_dir.name}/s{CHECKPOINT}/b0")
    assert teacher.errors == [], teacher.errors
    return Flow(source, replay, outcome, branch.require_ran(outcome))


def _moves(trace: RunTrace, steps: range) -> list[tuple[str, str] | None]:
    """Each step's recorded move as ``(reason, the URL it captured)``, or ``None``."""
    moved: list[tuple[str, str] | None] = []
    for step in steps:
        shot = trace.observation_fragment(step, "browser.screenshot")
        meta = trace.observation_fragment(step, "browser.browser_meta")
        assert isinstance(shot, ScreenshotObs) and isinstance(meta, BrowserMetaObs)
        assert shot.focus_moved == meta.focus_moved
        move = shot.focus_moved
        if move is not None:
            assert _active(meta) == [move.to_tab], "the tab moved to is the one captured"
        moved.append(None if move is None else (move.reason, meta.url))
    return moved


def test_the_recording_followed_the_tab(flow: Flow, mapped_site: FixtureSite) -> None:
    moves = _moves(flow.source.trace, range(len(RECORDING)))
    assert moves[0] is None and moves[2] is None
    assert moves[1] is not None and moves[1][0] == "new_tab"
    assert "/new_tab/target.html?how=replay" in moves[1][1]


def test_the_replay_follows_it_along_the_same_path(flow: Flow) -> None:
    recorded, replayed = flow.source.trace, flow.replay.trace
    steps = range(len(RECORDING))
    assert _moves(replayed, steps) == _moves(recorded, steps)
    assert driver.step_actions(replayed) == driver.step_actions(recorded)
    assert driver.step_signatures(replayed) == driver.step_signatures(recorded)


def test_a_branch_restores_the_focus_move(flow: Flow) -> None:
    records = flow.branch.step_records
    assert [record.origin for record in records][: CHECKPOINT + 1] == [
        "restored",
        "restored",
        "policy",
    ]
    for record in records[: CHECKPOINT + 1]:
        check = record.restoration
        assert check is not None and check.verdict == "ok", (record.step, check)
        (evidence,) = check.evidence
        assert evidence.url_match is True and evidence.signature_match is True
        assert evidence.screenshot_distance is not None and evidence.screenshot_distance <= 5
        assert evidence.document_misses == 0
    assert _moves(flow.branch, range(CHECKPOINT + 1)) == _moves(
        flow.source.trace, range(CHECKPOINT + 1)
    ), "the re-executed click moved the focus again, before the same observation"
    result = flow.branch_outcome.result
    assert result is not None and result.answer == "followed"
