"""Actual browser OS facts, setup assertions, fragments and replay identity (§4.13)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import ReplayLogArchiver, StorageArchiver
from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
from spectragent.core.capabilities.browser.observations import BrowserPlatformObs
from spectragent.core.capabilities.browser.platform import (
    PLATFORM_URL,
    PLATFORM_WORLD,
    measure_input_platform,
    platform_from_os_text,
)
from spectragent.core.capabilities.browser.profiles import BUILTIN_PROFILES
from spectragent.core.capabilities.browser.replay import ArchiveNetworkSource, PinnedReplayability
from spectragent.core.composer import ConfigComposer
from spectragent.scripts.generate_all import _collect_registrars
from tests._helpers.browser_archive import IsolatingBrowser, OwnedBrowser
from tests._helpers.fake_cdp import FakeBrowser
from tests._helpers.replay_archive import RecordingScript


@pytest.mark.parametrize(
    "text,expected",
    [
        ("macOS 版本27.0", "macos"),
        ("Mac OS X 15.0", "macos"),
        ("Windows 11", "windows"),
        ("Linux", "linux"),
        ("Linux Ubuntu 24.04", "linux"),
    ],
)
def test_internal_os_text_is_normalized(text, expected):
    assert platform_from_os_text(text) == expected


@pytest.mark.parametrize(
    "text", ["", "Android 15", "Chrome OS", "MacIntel", "Win32", "Darwin", "unknown"]
)
def test_unsupported_or_navigator_style_values_are_not_guessed(text):
    with pytest.raises(ValueError, match="unsupported browser"):
        platform_from_os_text(text)


@pytest.mark.parametrize(
    "os_text,expected", [("macOS", "macos"), ("Windows 11", "windows"), ("Linux", "linux")]
)
async def test_measurement_reads_only_the_owned_internal_os_cell_and_closes_it(os_text, expected):
    browser = FakeBrowser()
    browser.browser_os = os_text
    browser.user_agent = "intentionally unrelated UA"
    client = browser.client("ws://fake")
    await client.start()
    try:
        assert await measure_input_platform(client, timeout=0.2) == expected
        assert list(browser.targets) == ["page-1"]
        creates = [s for s in client.sent if s.method == "Target.createTarget"]
        assert creates[0].params == {"url": PLATFORM_URL, "background": True}
        worlds = [
            s.params.get("worldName") for s in client.sent if s.method == "Page.createIsolatedWorld"
        ]
        assert worlds == [PLATFORM_WORLD]
        assert not any(s.method == "Browser.getVersion" for s in client.sent)
    finally:
        await client.stop()


@pytest.mark.parametrize("value,error", [(None, TimeoutError), ("Android 15", ValueError)])
async def test_failed_measurement_closes_only_the_probe(value, error):
    browser = FakeBrowser()
    browser.browser_os = value
    client = browser.client("ws://fake")
    await client.start()
    try:
        with pytest.raises(error):
            await measure_input_platform(client, timeout=0.05)
        assert list(browser.targets) == ["page-1"]
    finally:
        await client.stop()


async def test_cancelled_measurement_closes_its_probe():
    browser = FakeBrowser()
    browser.browser_os = None
    client = browser.client("ws://fake")
    await client.start()
    task = asyncio.create_task(measure_input_platform(client, timeout=2))
    try:
        for _ in range(100):
            if browser.platform_contexts:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert list(browser.targets) == ["page-1"]
    finally:
        await client.stop()


async def test_repeated_cancellation_during_cleanup_still_closes_only_the_probe():
    browser = FakeBrowser()
    browser.browser_os = None
    release_close = browser.hold("Target.closeTarget")
    client = browser.client("ws://fake")
    await client.start()
    task = asyncio.create_task(measure_input_platform(client, timeout=2))
    try:
        for _ in range(100):
            if browser.platform_contexts:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        for _ in range(100):
            if any(s.method == "Target.closeTarget" for s in client.sent):
                break
            await asyncio.sleep(0.001)
        task.cancel()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert list(browser.targets) == ["page-1"]
    finally:
        release_close.set()
        await client.stop()


def _cap(browser, **kwargs):
    cap = BrowserCapability(settle_max_wait_s=0, **kwargs)
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    return cap


@pytest.mark.parametrize("profile", list(BUILTIN_PROFILES))
async def test_every_profile_provides_platform_without_dom_reads(profile):
    browser = FakeBrowser()
    cap = _cap(browser, profile=profile, expected_platform="macos")
    assert "browser.platform" in cap.declared_fragments
    try:
        await cap.setup(negotiation={"browser.platform"})
        assert cap._input_platform == "macos"
        before = len(browser.platform_contexts)
        for cycle in (1, 2):
            await cap.begin_observation(cycle)
            fragment = await cap.resolve_fragment("browser.platform")
            assert isinstance(fragment, BrowserPlatformObs)
            assert fragment.operating_system == "macos"
            assert fragment.fragment_id == "browser.platform"
        assert len(browser.platform_contexts) == before, (
            "platform is measured once, not per observation"
        )
        methods = [s.method for client in browser.clients for s in client.sent]
        assert "DOM.getDocument" not in methods and "DOMSnapshot.captureSnapshot" not in methods
        assert "Page.captureScreenshot" not in methods
    finally:
        await cap.teardown()
    assert cap._input_platform is None


async def test_expected_platform_is_an_assertion_before_any_business_navigation():
    browser = FakeBrowser()
    cap = _cap(browser, expected_platform="windows")
    try:
        with pytest.raises(BrowserCooperationError, match="expected_platform='windows'.*'macos'"):
            await cap.setup(start_url="https://example.com/task")
        assert not any(
            s.method == "Page.navigate" for client in browser.clients for s in client.sent
        )
        assert not cap._watchdogs
        assert list(browser.targets) == ["page-1"]
    finally:
        await cap.teardown()


async def test_unknown_actual_platform_refuses_setup_even_without_an_expectation():
    browser = FakeBrowser()
    browser.browser_os = "Android 15"
    cap = _cap(browser)
    try:
        with pytest.raises(BrowserCooperationError, match="input platform could not be measured"):
            await cap.setup()
    finally:
        await cap.teardown()


async def _source(base: Path, platform: Any):
    tier = PinnedReplayability(random_seed=7)
    record = RecordingScript(
        base,
        input_platform=platform,
        launch_flags=OwnedBrowser().planned_launch_flags(tier.launch_args(), tier.launch_env()),
    )
    await record.exchange("http://shop.test/", body=b"<html>home</html>")
    await record.commit(0)
    return record.finish()


@pytest.mark.parametrize("platform", [None, "unknown", "MacIntel", "android", False])
async def test_prepare_refuses_missing_or_invalid_source_platform(tmp_path, platform):
    source = await _source(tmp_path, platform)
    cap = _cap(FakeBrowser())
    cap.backend = IsolatingBrowser()  # type: ignore[assignment]
    cap.replayability = PinnedReplayability(random_seed=7)
    cap.network_source = ArchiveNetworkSource()
    with pytest.raises(BrowserCooperationError, match="input_platform"):
        cap.check_setup({"replay_archive": str(source)}, archivers=[ReplayLogArchiver()])
    assert not cap.backend.open_calls  # type: ignore[attr-defined]


async def test_replay_measures_actual_platform_and_refuses_a_different_one(tmp_path):
    source = await _source(tmp_path, "linux")
    browser = FakeBrowser()
    cap = _cap(browser)
    cap.backend = IsolatingBrowser()  # type: ignore[assignment]
    cap.replayability = PinnedReplayability(random_seed=7)
    cap.network_source = ArchiveNetworkSource()
    cap.bind_archivers([ReplayLogArchiver()])
    try:
        with pytest.raises(
            BrowserCooperationError, match="input_platform='linux'.*input_platform='macos'"
        ):
            await cap.setup(replay_archive=str(source))
        assert cap._recorder is None, "refuse before recorder target preparation"
    finally:
        await cap.teardown()


async def test_actual_platform_is_written_into_the_run_fingerprint():
    cap = _cap(FakeBrowser())
    cap.bind_archivers([StorageArchiver()])
    try:
        await cap.setup()
        batch = await cap.collect_archive()
        run = next(f for f in batch.files if f.archiver_id == "browser")
        assert json.loads(run.data)["input_platform"] == "macos"
    finally:
        await cap.teardown()


def test_expected_platform_config_composes_and_rejects_a_typo():
    import yaml

    path = Path(__file__).resolve().parents[3] / "configs/ui_tars_local.yaml"
    raw = yaml.safe_load(path.read_text())
    cap = raw["environment"][0]["capabilities"][0]
    cap["expected_platform"] = "linux"
    run = ConfigComposer(registrars=_collect_registrars()).compose(raw)
    environment = run.single("environment")
    assert next(iter(environment.capabilities.values())).expected_platform == "linux"
    cap["expected_platform"] = "linuz"
    with pytest.raises(ValidationError):
        ConfigComposer(registrars=_collect_registrars()).compose(raw)
