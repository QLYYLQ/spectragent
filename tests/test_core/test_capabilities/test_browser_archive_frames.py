"""``browser.frames`` — screenshots the agent never sees, for offline labels (§4.14).

Against the fake browser, through a real ``BrowserCapability``: a frame before
each of the agent's actions, captured on the recorder connection and waited
for before the action runs; a frame ``after_observation_delay_s`` after each
committed cycle, superseded by an action or a new observation that starts
first, and finished by a collection that finds it still waiting; failures and
timeouts as explicit index entries; a bounded wait for every action, released
at once on a tab whose captures keep timing out, and a boundary over its
budget that costs only its own frame (a ``timeout`` entry), never the
collection; every ``env.act`` call announced and numbered, a closed tab's
included, before the follow window opens; the index and its images read back
through the pure readers over a committed trace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from spectragent.core.archive import ArchiveBatch, ArchiveFile
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import (
    ActionBoundary,
    FramesArchiver,
    NetworkArchiver,
    ReplayLogArchiver,
)
from spectragent.core.capabilities.browser.archive.frames import (
    FRAMES_ARCHIVER_ID,
    FRAMES_FORMAT,
    FRAMES_INDEX_NAME,
    FrameRecord,
    FramesSession,
    iter_frames,
    read_frame_image,
    read_frames,
)
from spectragent.core.capabilities.browser.archive.redaction import MARKER_BROWSER_REDACTED
from spectragent.core.capabilities.browser.cooperation import (
    BrowserCooperationError,
    binding_problems,
)
from spectragent.core.capabilities.browser.replay import (
    ArchiveNetworkSource,
    PinnedReplayability,
)
from spectragent.core.capabilities.browser.replay.matcher import MatcherConfig
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.negotiation import negotiate_archive
from spectragent.core.trace.reader import RunTrace
from tests._helpers.archive_contract import ArchiveHarness
from tests._helpers.archive_recorder import open_session
from tests._helpers.browser_archive import (
    BrowserDriver,
    IsolatingBrowser,
    OwnedBrowser,
    archiving_capability,
    settle,
)
from tests._helpers.trace_builder import agent_result, step_record

#: The fake browser's screenshot: a real PNG, so its size is read from its header.
WIDTH, HEIGHT = 16, 9


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (WIDTH, HEIGHT), (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


async def _frames(**knobs: Any) -> tuple[Any, BrowserDriver, FramesArchiver]:
    archiver = FramesArchiver(**knobs)
    cap, driver = archiving_capability(archivers=(archiver,), fragments=frozenset({"browser.dom"}))
    driver.browser.screenshot = _png()
    await driver.start()
    return cap, driver, archiver


def _index(batch: ArchiveBatch) -> list[dict[str, Any]]:
    (item,) = [f for f in batch.files if f.name == FRAMES_INDEX_NAME]
    document = json.loads(item.data)
    assert document["format"] == FRAMES_FORMAT
    return document["frames"]


def _images(batch: ArchiveBatch) -> dict[str, ArchiveFile]:
    return {
        f.name: f
        for f in batch.files
        if f.archiver_id == FRAMES_ARCHIVER_ID and f.name != FRAMES_INDEX_NAME
    }


def _frame_files(batch: ArchiveBatch) -> list[ArchiveFile]:
    return [f for f in batch.files if f.archiver_id == FRAMES_ARCHIVER_ID]


def _recorder_screenshots(driver: BrowserDriver) -> list[Any]:
    return [s for s in driver.recorder.sent if s.method == "Page.captureScreenshot"]


class TestBeforeAction:
    async def test_one_frame_per_action_on_the_recorder_connection(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            control_before = len(
                [s for s in driver.browser.control.sent if s.method == "Page.captureScreenshot"]
            )
            await driver.observe()
            started = time.time()
            await driver.act()
            await driver.act("browser.wait", seconds=0)
            batch = await cap.collect_archive()
            frames = _index(batch)
            assert [(f["kind"], f["cycle"], f["action_index"], f["action"]) for f in frames] == [
                ("before_action", 1, 0, "browser.wait"),
                ("before_action", 1, 1, "browser.wait"),
            ]
            for frame in frames:
                assert frame["outcome"] == "captured" and frame["tab"] == "page-1"
                assert (frame["width"], frame["height"]) == (WIDTH, HEIGHT)
                assert started <= frame["requested_at"] <= frame["captured_at"] <= time.time()
                assert frame["superseded_by"] is None and frame["detail"] is None
            images = _images(batch)
            assert sorted(images) == ["frame-1-before-0.png", "frame-1-before-1.png"]
            for frame in frames:
                image = images[frame["name"]]
                assert image.content_addressed and image.media_type == "image/png"
                assert hashlib.sha256(image.data).hexdigest() == frame["sha256"]
                assert image.meta.cycle == 1 and dict(image.meta.ids)["kind"] == "before_action"
            # Taken on the recorder's session for the tab, as a clean PNG of the viewport.
            shots = _recorder_screenshots(driver)
            assert [(s.params, s.session_id) for s in shots] == [
                ({"format": "png"}, driver.page_session())
            ] * 2
            assert (
                len(
                    [s for s in driver.browser.control.sent if s.method == "Page.captureScreenshot"]
                )
                == control_before
            ), "the control connection takes no frame"
            # Handed over and forgotten.
            assert (await cap.collect_archive()).files == ()
        finally:
            await cap.teardown()

    async def test_the_action_waits_for_its_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            await driver.observe()
            ran: list[float] = []
            original = cap._run_action

            async def spy(action: Any) -> Any:
                ran.append(time.time())
                return await original(action)

            monkeypatch.setattr(cap, "_run_action", spy)
            held = driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            acting = asyncio.ensure_future(driver.act())
            await settle()
            assert _recorder_screenshots(driver), "the frame was requested"
            assert ran == [], "the action ran before its frame was captured"
            held.set()
            result = await acting
            assert result.success and len(ran) == 1
            (frame,) = _index(await cap.collect_archive())
            assert frame["outcome"] == "captured" and frame["captured_at"] <= ran[0]
            assert frame["requested_at"] <= frame["captured_at"] <= frame["released_at"] <= ran[0]
        finally:
            await cap.teardown()

    async def test_the_follow_window_opens_after_the_frame(self) -> None:
        """A tab the page opens while the archive captures is not the agent's to follow."""
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            await driver.observe()
            held = driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            acting = asyncio.ensure_future(driver.act())
            await settle()
            assert cap._follow_window is None, "opened before the frame was taken"
            held.set()
            assert (await acting).success
            assert cap._follow_window is not None
        finally:
            await cap.teardown()

    async def test_the_index_restarts_at_each_cycle(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            await driver.observe()
            await driver.act()
            await driver.act()
            await driver.observe()
            await driver.act()
            frames = _index(await cap.collect_archive())
            assert [(f["cycle"], f["action_index"]) for f in frames] == [(1, 0), (1, 1), (2, 0)]
            assert [f["name"] for f in frames] == [
                "frame-1-before-0.png",
                "frame-1-before-1.png",
                "frame-2-before-0.png",
            ]
        finally:
            await cap.teardown()

    async def test_off_takes_no_frame_before_an_action(self) -> None:
        cap, driver, _ = await _frames(before_action=False, after_observation_delay_s=None)
        try:
            await driver.observe()
            await driver.act()
            assert _frame_files(await cap.collect_archive()) == []
            assert _recorder_screenshots(driver) == []
        finally:
            await cap.teardown()

    async def test_an_unbound_archiver_slows_no_action(self) -> None:
        cap, driver, archiver = await _frames(after_observation_delay_s=None)
        try:
            cap.bind_archivers([])
            await driver.observe()
            await driver.act()
            assert _recorder_screenshots(driver) == []
            cap.bind_archivers([archiver])
            await driver.act()
            assert len(_recorder_screenshots(driver)) == 1
            (frame,) = _index(await cap.collect_archive())
            assert frame["action_index"] == 1, "the call made while unbound still counts"
        finally:
            await cap.teardown()


class TestEveryCallIsAnnounced:
    """``action_index`` is the ordinal of the ``env.act`` call since the cycle."""

    async def test_an_action_on_a_tab_that_closed_itself(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            await driver.observe()
            driver.browser.close_target("page-1")
            result = await driver.act()
            assert result.success is False and "closed itself" in (result.error or "")
            assert cap._follow_window is None
            (frame,) = _index(await cap.collect_archive())
            assert (frame["action_index"], frame["outcome"], frame["tab"]) == (
                0,
                "no_tab",
                "page-1",
            )
            assert "closed itself" in frame["detail"]
            assert frame["released_at"] is not None
            assert _recorder_screenshots(driver) == []
        finally:
            await cap.teardown()

    async def test_a_tab_that_closes_while_its_frame_is_taken(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            await driver.observe()
            held = driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            acting = asyncio.ensure_future(driver.act())
            await settle()
            driver.browser.close_target("page-1")
            await settle()
            held.set()
            result = await acting
            assert result.success is False and "closed itself" in (result.error or "")
            assert cap._follow_window is None, "no follow window for an action that never ran"
            (frame,) = _index(await cap.collect_archive())
            assert frame["action_index"] == 0
        finally:
            await cap.teardown()

    async def test_the_ordinals_match_the_calls_whatever_became_of_them(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None, capture_timeout_s=0.05)
        try:
            await driver.observe()
            await driver.act()
            held = driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            await driver.act()
            held.set()
            driver.browser.fail(
                "Page.captureScreenshot", RuntimeError("busy"), session_id=driver.page_session()
            )
            await driver.act()
            frames = _index(await cap.collect_archive())
            assert [(f["action_index"], f["outcome"]) for f in frames] == [
                (0, "captured"),
                (1, "timeout"),
                (2, "failed"),
            ]
        finally:
            await cap.teardown()


class TestAfterObservation:
    async def test_captured_after_the_delay_when_nothing_comes_first(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=0.05)
        try:
            await driver.observe()
            committed = time.time()
            await asyncio.sleep(0.2)
            batch = await cap.collect_archive()
            (frame,) = _index(batch)
            assert (frame["kind"], frame["cycle"], frame["outcome"]) == (
                "after_observation",
                1,
                "captured",
            )
            assert frame["action_index"] is None and frame["action"] is None
            assert frame["requested_at"] == pytest.approx(committed + 0.05, abs=0.05)
            assert frame["captured_at"] >= frame["requested_at"]
            assert frame["name"] == "frame-1-after.png" and frame["name"] in _images(batch)
        finally:
            await cap.teardown()

    async def test_an_action_before_the_delay_supersedes_it(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=5.0)
        try:
            await driver.observe()
            await driver.act()
            started = time.time()
            batch = await cap.collect_archive()
            assert time.time() - started < 1.0, "a superseded frame is not waited for"
            superseded, before = _index(batch)
            assert (superseded["kind"], superseded["outcome"], superseded["superseded_by"]) == (
                "after_observation",
                "superseded",
                "action",
            )
            assert superseded["name"] is None and superseded["captured_at"] is None
            assert superseded["sha256"] is None and superseded["width"] is None
            assert before["kind"] == "before_action" and before["outcome"] == "captured"
            assert list(_images(batch)) == ["frame-1-before-0.png"]
        finally:
            await cap.teardown()

    async def test_a_new_observation_before_the_delay_supersedes_it(self) -> None:
        cap, driver, _ = await _frames(before_action=False, after_observation_delay_s=5.0)
        try:
            await driver.observe()
            await driver.observe()
            await driver.act()
            frames = _index(await cap.collect_archive())
            assert [(f["cycle"], f["outcome"], f["superseded_by"]) for f in frames] == [
                (1, "superseded", "observation"),
                (2, "superseded", "action"),
            ]
        finally:
            await cap.teardown()

    async def test_a_frame_already_due_is_taken_before_the_action(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=0.05)
        try:
            await driver.observe()
            await asyncio.sleep(0.15)
            await driver.act()
            after, before = _index(await cap.collect_archive())
            assert (after["kind"], after["outcome"]) == ("after_observation", "captured")
            assert (before["kind"], before["outcome"]) == ("before_action", "captured")
            assert after["captured_at"] <= before["captured_at"]
        finally:
            await cap.teardown()

    async def test_a_collection_finishes_a_waiting_frame(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=0.3)
        try:
            await driver.observe()
            started = time.time()
            batch = await cap.collect_archive()
            waited = time.time() - started
            (frame,) = _index(batch)
            assert frame["outcome"] == "captured", "the frame of this step's observation"
            assert 0.1 < waited < 0.3 + 3.0
            assert (await cap.collect_archive()).files == ()
        finally:
            await cap.teardown()

    async def test_a_collection_waits_the_rest_of_the_delay_plus_the_capture_timeout(
        self,
    ) -> None:
        """No more than that; a frame still in flight lands, once, in a later batch."""
        cap, driver, _ = await _frames(
            before_action=False, after_observation_delay_s=0.05, capture_timeout_s=0.1
        )
        try:
            driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            await driver.observe()
            started = time.perf_counter()
            first = _frame_files(await cap.collect_archive())
            assert time.perf_counter() - started < 0.05 + 0.1 + 0.05
            await asyncio.sleep(0.2)
            second = _frame_files(await cap.collect_archive())
            frames = [
                frame
                for files in (first, second)
                for item in files
                if item.name == FRAMES_INDEX_NAME
                for frame in json.loads(item.data)["frames"]
            ]
            assert [(f["kind"], f["outcome"]) for f in frames] == [("after_observation", "timeout")]
            assert "0.1s" in frames[0]["detail"]
        finally:
            await cap.teardown()

    async def test_off_schedules_nothing(self) -> None:
        cap, driver, _ = await _frames(before_action=False, after_observation_delay_s=None)
        try:
            await driver.observe()
            await asyncio.sleep(0.05)
            assert _frame_files(await cap.collect_archive()) == []
            assert _recorder_screenshots(driver) == []
        finally:
            await cap.teardown()

    async def test_teardown_stops_a_waiting_frame(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=30.0)
        await driver.start()
        await driver.observe()
        (session,) = cap._archive.sessions
        assert isinstance(session, FramesSession) and session._tasks
        tasks = set(session._tasks)
        await cap.teardown()
        assert all(task.done() for task in tasks)
        assert _recorder_screenshots(driver) == []


class TestFailuresAreListed:
    async def test_a_capture_that_times_out(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None, capture_timeout_s=0.05)
        try:
            await driver.observe()
            held = driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            result = await driver.act()
            assert result.success, "the action runs whatever became of its frame"
            batch = await cap.collect_archive()
            (frame,) = _index(batch)
            assert frame["outcome"] == "timeout" and "0.05s" in frame["detail"]
            assert frame["name"] is None and frame["captured_at"] is None
            assert frame["released_at"] - frame["requested_at"] == pytest.approx(0.05, abs=0.1)
            assert _images(batch) == {}
            held.set()
        finally:
            await cap.teardown()

    async def test_a_capture_that_fails(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            await driver.observe()
            driver.browser.fail(
                "Page.captureScreenshot",
                RuntimeError("Unable to capture screenshot"),
                session_id=driver.page_session(),
            )
            assert (await driver.act()).success
            (frame,) = _index(await cap.collect_archive())
            assert frame["outcome"] == "failed"
            assert frame["detail"] == "RuntimeError: Unable to capture screenshot"
        finally:
            await cap.teardown()

    async def test_a_reply_without_an_image(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            await driver.observe()
            driver.browser.screenshot = b""
            await driver.act()
            (frame,) = _index(await cap.collect_archive())
            assert frame["outcome"] == "failed" and "no image data" in frame["detail"]
        finally:
            await cap.teardown()

    async def test_a_tab_the_recorder_does_not_know(self) -> None:
        session, recorder = open_session(FramesArchiver(after_observation_delay_s=None))
        recorder.add_target("page-s", "page-1")
        for index, target in enumerate(("gone", None)):
            await session.on_action_boundary(
                ActionBoundary(
                    target_id=target,
                    control_session_id="c",
                    action="browser.click",
                    requested_at=1.0 + index,
                    cycle=1,
                    action_index=index,
                )
            )
        files = await session.collect()
        (index,) = files
        frames = json.loads(index.data)["frames"]
        assert [(f["outcome"], f["tab"], f["action_index"]) for f in frames] == [
            ("no_tab", "gone", 0),
            ("no_tab", "", 1),
        ]
        assert "gone" in frames[0]["detail"] and "(unknown)" in frames[1]["detail"]
        assert recorder.methods() == []

    async def test_a_cancelled_action_still_lists_its_frame(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            await driver.observe()
            held = driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            acting = asyncio.ensure_future(driver.act())
            await settle()
            acting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await acting
            (frame,) = _index(await cap.collect_archive())
            assert (frame["action_index"], frame["outcome"]) == (0, "timeout")
            assert frame["detail"].startswith("the action stopped waiting after")
            held.set()
        finally:
            await cap.teardown()

    async def test_a_boundary_over_its_budget_costs_only_its_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run cancels a session past its budget: the frame is a timeout, the step is whole."""
        cap, driver, _ = await _frames(after_observation_delay_s=None, max_action_wait_s=0.05)
        try:
            await driver.observe()
            monkeypatch.setattr(
                "spectragent.core.capabilities.browser.archive.run.ACTION_BOUNDARY_GRACE_S", 0.0
            )

            async def hang(self: FramesSession, boundary: ActionBoundary, deadline: float) -> Any:
                await asyncio.Event().wait()

            monkeypatch.setattr(FramesSession, "_before_action", hang)
            started = time.time()
            result = await driver.act()
            assert result.success and time.time() - started < 1.0
            batch = await cap.collect_archive()  # does not raise
            (frame,) = _index(batch)
            assert (frame["action_index"], frame["outcome"]) == (0, "timeout")
            assert frame["detail"].startswith("the action stopped waiting after")
            assert frame["released_at"] >= frame["requested_at"]
            assert cap._archive is not None and cap._archive.channel.errors == 0
        finally:
            await cap.teardown()


class TestTheActionsWaitIsBounded:
    async def test_a_slow_capture_holds_the_action_at_most_max_action_wait_s(self) -> None:
        cap, driver, _ = await _frames(
            after_observation_delay_s=None, capture_timeout_s=5.0, max_action_wait_s=0.1
        )
        try:
            await driver.observe()
            held = driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            started = time.perf_counter()
            assert (await driver.act()).success
            assert time.perf_counter() - started < 0.6
            (frame,) = _index(await cap.collect_archive())
            assert frame["outcome"] == "timeout" and "action's wait (0.1s)" in frame["detail"]
            held.set()
        finally:
            await cap.teardown()

    async def test_a_due_frame_and_the_actions_own_share_one_wait(self) -> None:
        cap, driver, _ = await _frames(
            after_observation_delay_s=0.02, capture_timeout_s=5.0, max_action_wait_s=0.2
        )
        try:
            held = driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            await driver.observe()
            await asyncio.sleep(0.1)  # the after-observation frame is due and stuck
            started = time.perf_counter()
            assert (await driver.act()).success
            assert time.perf_counter() - started < 0.7
            after, before = _index(await cap.collect_archive())
            assert (after["kind"], after["outcome"]) == ("after_observation", "timeout")
            assert "action's wait" in after["detail"] and after["released_at"] is None
            assert (before["kind"], before["outcome"]) == ("before_action", "timeout")
            held.set()
        finally:
            await cap.teardown()

    async def test_after_two_timeouts_a_tab_is_not_waited_on_until_a_capture_succeeds(
        self,
    ) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None, capture_timeout_s=0.05)
        try:
            await driver.observe()
            held = driver.browser.hold("Page.captureScreenshot", session_id=driver.page_session())
            await driver.act()
            await driver.act()
            sent = len(_recorder_screenshots(driver))
            started = time.perf_counter()
            await driver.act()
            assert time.perf_counter() - started < 0.04, "not waited for"
            assert len(_recorder_screenshots(driver)) == sent + 1, "a probe went out"
            await driver.act()
            assert len(_recorder_screenshots(driver)) == sent + 1, "one probe at a time"
            held.set()  # the tab answers again: the probe succeeds
            await settle()
            await driver.act()
            frames = _index(await cap.collect_archive())
            assert [f["outcome"] for f in frames] == [
                "timeout",
                "timeout",
                "timeout",
                "timeout",
                "captured",
            ]
            assert all(f["detail"].startswith("not waited for") for f in frames[2:4])
        finally:
            await cap.teardown()


class TestCooperation:
    """Frames fit every network source: recording, and an offline replay or branch."""

    def test_a_live_recording_binds_it_beside_the_others(self) -> None:
        cap = BrowserCapability()
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
        cap.replayability = PinnedReplayability(random_seed=7)
        cap.bind_archivers([NetworkArchiver(), FramesArchiver()])
        assert [a.archiver_id for a in cap.bound_archivers] == ["browser.network", "browser.frames"]

    def test_a_replay_binds_it_beside_the_replay_log(self) -> None:
        cap = BrowserCapability()
        cap.backend = IsolatingBrowser()  # type: ignore[assignment]
        cap.replayability = PinnedReplayability(random_seed=7)
        cap.network_source = ArchiveNetworkSource(
            matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules")
        )
        cap.bind_archivers([ReplayLogArchiver(), FramesArchiver()])
        assert binding_problems(cap, cap.bound_archivers, cdp_url=None, injected_client=False) == []

    def test_it_needs_no_replayable_tier_and_declares_no_session_state(self) -> None:
        frames = FramesArchiver()
        assert frames.needs == frozenset({"action_boundary"})
        assert frames.requires_replayable is False
        assert frames.privacy_markers == frozenset({MARKER_BROWSER_REDACTED})
        cap = BrowserCapability()
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
        cap.bind_archivers([frames])  # the none tier
        refused = BrowserCapability()
        refused.backend = OwnedBrowser(profile="/profiles/me")  # type: ignore[assignment]
        with pytest.raises(BrowserCooperationError, match="allow_session_state"):
            refused.bind_archivers([frames])
        refused.bind_archivers([FramesArchiver(allow_session_state=True)])


class TestTheIndex:
    def test_the_record_has_the_contract_fields_in_order(self) -> None:
        assert list(FrameRecord.__dataclass_fields__) == [
            "kind",
            "cycle",
            "action_index",
            "action",
            "tab",
            "requested_at",
            "captured_at",
            "released_at",
            "outcome",
            "superseded_by",
            "name",
            "sha256",
            "width",
            "height",
            "detail",
        ]

    async def test_the_index_is_stamped_inside_the_window_and_scoped_to_the_step(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=5.0)
        try:
            started = time.time()
            await driver.observe()
            await driver.act()
            batch = await cap.collect_archive()
            (index,) = [f for f in batch.files if f.name == FRAMES_INDEX_NAME]
            assert index.scope == "step" and not index.content_addressed
            assert index.media_type == "application/json"
            assert started <= index.meta.captured_at <= time.time()
        finally:
            await cap.teardown()

    async def test_identical_frames_are_stored_once(self) -> None:
        cap, driver, _ = await _frames(after_observation_delay_s=None)
        try:
            await driver.observe()
            await driver.act()
            await driver.act()
            images = _images(await cap.collect_archive())
            assert len(images) == 2 and len({i.data for i in images.values()}) == 1
            assert all(i.content_addressed for i in images.values())
        finally:
            await cap.teardown()


class TestTheReaders:
    async def test_frames_and_images_read_back_from_a_committed_trace(self, tmp_path: Path) -> None:
        archiver = FramesArchiver(after_observation_delay_s=0.05)
        cap, driver = archiving_capability(archivers=(archiver,))
        driver.browser.screenshot = _png()
        harness = ArchiveHarness(
            capability=cap,
            archivers=(archiver,),
            produce=driver.produce,
            break_collect=driver.break_collect,
            close=driver.close,
        )
        env = harness.environment()
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"), archive=[archiver])
        negotiation = negotiate_archive([writer], env)
        env.bind_archivers(writer.archive)
        await driver.ensure_setup()
        await writer.on_run_start(
            env=env, task="frames", config={}, archive_negotiation=negotiation
        )
        try:
            # Step 0: an action supersedes the after-observation frame.
            await driver.observe()
            await driver.act()
            await writer.after_record_step(None, args=(step_record(0),), kwargs={})
            await writer.flush()
            # Step 1: no action — the after-observation frame is taken.
            await driver.observe()
            await asyncio.sleep(0.15)
            await writer.after_record_step(None, args=(step_record(1),), kwargs={})
            await writer.flush()
            # The run ends on an observation whose frame is still waiting.
            await driver.observe()
            await writer.on_run_end(result=agent_result())
        finally:
            await harness.aclose()

        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        step0 = read_frames(trace, 0)
        assert [(f.kind, f.outcome, f.superseded_by) for f in step0] == [
            ("after_observation", "superseded", "action"),
            ("before_action", "captured", None),
        ]
        (step1,) = read_frames(trace, 1)
        assert (step1.kind, step1.cycle, step1.outcome) == ("after_observation", 2, "captured")
        assert read_frames(trace, 5) == []

        everything = list(iter_frames(trace))
        assert [(step, f.cycle, f.kind) for step, f in everything] == [
            (0, 1, "after_observation"),
            (0, 1, "before_action"),
            (1, 2, "after_observation"),
            (None, 3, "after_observation"),
        ]
        assert everything[-1][1].outcome == "captured", "the last collection finished it"
        assert [f.cycle for _, f in iter_frames(trace, include_trailing=False)] == [1, 1, 2]

        for step, frame in everything:
            if frame.outcome != "captured":
                with pytest.raises(ValueError, match="has no image"):
                    read_frame_image(trace, step, frame)
                continue
            image = read_frame_image(trace, step, frame)
            assert hashlib.sha256(image).hexdigest() == frame.sha256
            assert Image.open(BytesIO(image)).size == (frame.width, frame.height)
        with pytest.raises(KeyError, match="holds no browser.frames image"):
            read_frame_image(trace, 1, step0[1])
