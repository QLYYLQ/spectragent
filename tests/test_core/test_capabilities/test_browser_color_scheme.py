"""A measured recording theme, inherited by replay instead of re-reading its host (§4.14)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import ReplayLogArchiver, StorageArchiver
from spectragent.core.capabilities.browser.color_scheme import (
    COLOR_SCHEME_WORLD,
    measure_color_scheme,
)
from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
from spectragent.core.capabilities.browser.recorder import RecorderTarget
from spectragent.core.capabilities.browser.replay import (
    ArchiveNetworkSource,
    NoneReplayability,
    PinnedReplayability,
)
from spectragent.core.capabilities.browser.replay.matcher import MatcherConfig
from tests._helpers.browser_archive import IsolatingBrowser, OwnedBrowser
from tests._helpers.fake_cdp import FakeBrowser
from tests._helpers.replay_archive import RecordingScript


def _cap(browser: FakeBrowser, *, replay: bool = False) -> BrowserCapability:
    cap = BrowserCapability()
    cap.backend = IsolatingBrowser() if replay else OwnedBrowser()  # type: ignore[assignment]
    cap.replayability = PinnedReplayability(random_seed=7)
    if replay:
        cap.network_source = ArchiveNetworkSource(
            matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules")
        )
        cap.bind_archivers([ReplayLogArchiver()])
    cap._inject_cdp_factory(browser.client)
    return cap


async def _recorded(tmp_path: Path, scheme: Any) -> Path:
    tier = PinnedReplayability(random_seed=7)
    script = RecordingScript(
        tmp_path,
        launch_flags=OwnedBrowser().planned_launch_flags(tier.launch_args(), tier.launch_env()),
    )
    # These malformed facts exercise what prepare reads from an archive, not
    # what the measured production plan is allowed to construct.
    script._plan = dataclasses.replace(script._plan, color_scheme=scheme)
    await script.exchange("http://shop.test/", body=b"<html>theme</html>")
    await script.commit(0)
    return script.finish()


@pytest.mark.parametrize("scheme", ["light", "dark"])
async def test_startup_measurement_uses_an_isolated_world_and_detaches(scheme: str) -> None:
    browser = FakeBrowser()
    browser.color_scheme = scheme
    client = browser.client("ws://fake")
    await client.start()
    try:
        assert await measure_color_scheme(client, timeout=1, attempt_timeout=1) == scheme
        worlds = [s for s in client.sent if s.method == "Page.createIsolatedWorld"]
        assert worlds[0].params["worldName"] == COLOR_SCHEME_WORLD
        read = next(s for s in client.sent if s.method == "Runtime.evaluate")
        assert read.params["contextId"] in browser.color_scheme_contexts
        assert client.sent[-1].method == "Target.detachFromTarget"
    finally:
        await client.stop()


async def test_a_read_answered_late_is_made_again() -> None:
    """A renderer slow under load: the first attempt times out, the next is answered."""
    browser = FakeBrowser()
    browser.color_scheme = "dark"
    browser.delay("Runtime.evaluate", 0.5)
    client = browser.client("ws://fake")
    await client.start()
    try:
        assert await measure_color_scheme(client, timeout=5, attempt_timeout=0.1) == "dark"
        assert client.methods().count("Target.attachToTarget") == 2
        assert client.methods().count("Target.detachFromTarget") == 2
    finally:
        await client.stop()


async def test_a_page_that_never_answers_fails_naming_its_budget() -> None:
    browser = FakeBrowser()
    never = browser.hold("Runtime.evaluate")
    client = browser.client("ws://fake")
    await client.start()
    try:
        with pytest.raises(TimeoutError, match=r"no answer within 0\.3s \(\d+ attempts"):
            await measure_color_scheme(client, timeout=0.3, attempt_timeout=0.1)
        assert client.methods().count("Target.attachToTarget") >= 2
    finally:
        never.set()
        await client.stop()


async def test_an_ambiguous_answer_is_not_asked_again() -> None:
    browser = FakeBrowser()
    browser.color_scheme = None
    client = browser.client("ws://fake")
    await client.start()
    try:
        with pytest.raises(RuntimeError, match="no valid prefers-color-scheme"):
            await measure_color_scheme(client, timeout=5, attempt_timeout=1)
        assert client.methods().count("Runtime.evaluate") == 1
    finally:
        await client.stop()


@pytest.mark.parametrize("scheme", [None, "", "auto", "Dark", False, {"color": "dark"}])
async def test_an_unmeasurable_theme_refuses_recording_before_target_preparation(
    scheme: Any,
) -> None:
    browser = FakeBrowser()
    browser.color_scheme = scheme
    cap = _cap(browser)
    try:
        with pytest.raises(
            BrowserCooperationError, match="prefers-color-scheme could not be measured"
        ):
            await cap.setup()
        assert "Target.setAutoAttach" not in browser.recorder.methods()
        assert "Emulation.setEmulatedMedia" not in browser.recorder.methods()
        assert browser.recorder.sent[-1].method == "Target.detachFromTarget"
    finally:
        await cap.teardown()


@pytest.mark.parametrize("scheme", ["light", "dark"])
async def test_recording_pins_the_measured_value_and_records_it(scheme: str) -> None:
    browser = FakeBrowser()
    browser.color_scheme = scheme
    cap = _cap(browser)
    cap.bind_archivers([StorageArchiver()])
    try:
        await cap.setup()
        assert cap._replay_plan is not None
        assert cap._replay_plan.color_scheme == scheme
        commands = browser.recorder.sent
        read_index = next(
            i
            for i, s in enumerate(commands)
            if s.method == "Runtime.evaluate"
            and s.params.get("contextId") in browser.color_scheme_contexts
        )
        prepared_index = next(
            i for i, s in enumerate(commands) if s.method == "Target.setAutoAttach"
        )
        assert read_index < prepared_index
        browser.color_scheme = "light" if scheme == "dark" else "dark"
        assert cap._replay_plan.fingerprint()["color_scheme"] == scheme
        batch = await cap.collect_archive()
        fingerprint = next(f for f in batch.files if f.archiver_id == "browser")
        assert json.loads(fingerprint.data)["replayability"]["color_scheme"] == scheme
    finally:
        await cap.teardown()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("reduced", [True, False])
def test_one_media_command_preserves_theme_and_motion_on_every_frame(
    scheme: Any, reduced: bool
) -> None:
    plan = PinnedReplayability(reduced_motion=reduced).plan(
        t0_ms=1, browser_user_agent="Browser", color_scheme=scheme
    )
    expected = [{"name": "prefers-color-scheme", "value": scheme}]
    if reduced:
        expected.append({"name": "prefers-reduced-motion", "value": "reduce"})
    for kind in ("page", "iframe"):
        target = RecorderTarget(session_id="s", target_id="t", type=kind)
        media = [
            c for c in plan.target_commands(target) if c.method == "Emulation.setEmulatedMedia"
        ]
        assert len(media) == 1 and media[0].params == {"features": expected}
    assert plan.fingerprint()["color_scheme"] == scheme
    assert "color_scheme" not in plan.params, "it is measured run evidence, not a YAML knob"


@pytest.mark.parametrize("source_theme,host_theme", [("dark", "light"), ("light", "dark")])
async def test_replay_inherits_source_theme_without_measuring_its_host(
    tmp_path: Path, source_theme: str, host_theme: str
) -> None:
    source = await _recorded(tmp_path, source_theme)
    browser = FakeBrowser()
    browser.color_scheme = host_theme
    cap = _cap(browser, replay=True)
    try:
        await cap.setup(replay_archive=str(source))
        assert not browser.color_scheme_contexts
        assert cap._replay_plan is not None
        assert cap._replay_plan.color_scheme == source_theme
        batch = await cap.collect_archive()
        fingerprint = next(f for f in batch.files if f.archiver_id == "browser")
        assert json.loads(fingerprint.data)["replayability"]["color_scheme"] == source_theme
    finally:
        await cap.teardown()


@pytest.mark.parametrize("scheme", [None, "auto", "", False, 1, ["dark"], {"value": "light"}])
async def test_prepare_refuses_missing_or_invalid_recorded_theme_before_launch(
    tmp_path: Path, scheme: Any
) -> None:
    source = await _recorded(tmp_path, scheme)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    browser = FakeBrowser()
    cap = _cap(browser, replay=True)
    with pytest.raises(BrowserCooperationError, match="replayability.color_scheme"):
        cap.check_setup({"replay_archive": str(source)}, negotiation=(), archivers=cap._archivers)
    assert not browser.clients
    assert cap.backend is not None and not cap.backend.open_calls  # type: ignore[attr-defined]
    assert before == {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }


async def test_none_keeps_the_host_theme_even_with_an_archive() -> None:
    browser = FakeBrowser()
    cap = _cap(browser)
    cap.replayability = NoneReplayability()
    cap.bind_archivers([StorageArchiver()])
    try:
        await cap.setup()
        assert not browser.color_scheme_contexts
        assert "Emulation.setEmulatedMedia" not in browser.recorder.methods()
        assert cap._replay_plan is not None
        assert "color_scheme" not in cap._replay_plan.fingerprint()
    finally:
        await cap.teardown()
