"""Real Chrome platform facts are independent of UA and checked before business targets."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import NetworkArchiver, ReplayLogArchiver
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
from spectragent.core.capabilities.browser.platform import PLATFORM_URL, measure_input_platform
from spectragent.core.capabilities.browser.replay import ArchiveNetworkSource, PinnedReplayability
from spectragent.core.trace.reader import RunTrace
from tests._helpers.replay_archive import RecordingScript

pytestmark = pytest.mark.requires_chrome
WINDOWS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="This canary requires a real macOS Chrome, not a spoofed platform",
)
async def test_mac_os_fact_survives_both_launch_and_renderer_windows_identities():
    provider = LocalChromeProvider(headless=True, user_agent=WINDOWS_UA)
    session = await provider.open()
    assert session.cdp_url is not None
    client = CDPClient(session.cdp_url, proxy=None, compression=None)
    seen = []
    try:
        await client.start()
        version = await client.send("Browser.getVersion", {}, timeout=5)
        assert "Windows" in version["userAgent"]

        class OverriddenRenderer:
            async def send(self, method, params=None, session_id=None, *, timeout=None):
                result = await client.send(method, params, session_id, timeout=timeout)
                if method == "Target.attachToTarget":
                    sid = result["sessionId"]
                    await client.send(
                        "Emulation.setUserAgentOverride",
                        {
                            "userAgent": WINDOWS_UA,
                            "platform": "Win32",
                            "userAgentMetadata": {
                                "brands": [],
                                "platform": "Windows",
                                "platformVersion": "10.0.0",
                                "architecture": "x86",
                                "model": "",
                                "mobile": False,
                            },
                        },
                        sid,
                        timeout=5,
                    )
                    read = await client.send(
                        "Runtime.evaluate",
                        {
                            "expression": "navigator.platform",
                            "returnByValue": True,
                        },
                        sid,
                        timeout=5,
                    )
                    seen.append(read["result"]["value"])
                return result

        assert await measure_input_platform(OverriddenRenderer()) == "macos"
        assert seen == ["Win32"]
        targets = await client.send("Target.getTargets", {}, timeout=5)
        assert all(
            t.get("url", "").rstrip("/") != PLATFORM_URL.rstrip("/") for t in targets["targetInfos"]
        )
    finally:
        await client.stop()
        await provider.close(session)


async def test_expected_platform_mismatch_refuses_real_chrome_before_business_navigation():
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)
    try:
        await cap.setup(negotiation={"browser.platform"})
        actual = cap._input_platform
        assert actual in ("macos", "windows", "linux")
    finally:
        await cap.teardown()
    other = "windows" if actual != "windows" else "linux"
    cap = BrowserCapability(expected_platform=other)
    cap.backend = LocalChromeProvider(headless=True)
    try:
        with pytest.raises(BrowserCooperationError, match="expected_platform"):
            await cap.setup(start_url="https://must-not-navigate.example.test/")
        assert cap._input_platform == actual
        assert not cap._watchdogs
        targets = await cap.cdp.send("Target.getTargets", {}, timeout=5)
        assert all("must-not-navigate" not in t.get("url", "") for t in targets["targetInfos"])
    finally:
        await cap.teardown()


async def test_cdp_provider_that_refuses_the_internal_page_is_not_given_a_guessed_platform():
    class RefusedInternalPage(CDPClient):
        async def send(self, method, params=None, session_id=None, *, timeout=None):
            if method == "Target.createTarget" and (params or {}).get("url") == PLATFORM_URL:
                raise RuntimeError("test provider refuses internal pages")
            return await super().send(method, params, session_id, timeout=timeout)

    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)
    cap._inject_cdp_factory(RefusedInternalPage)
    try:
        with pytest.raises(BrowserCooperationError, match="input platform could not be measured"):
            await cap.setup()
        assert cap._input_platform is None and not cap._watchdogs
    finally:
        await cap.teardown()


async def test_real_replay_rejects_source_evidence_for_a_different_platform(tmp_path: Path):
    """The source is a WAL fixture with real launch facts and a counterfactual OS.

    No Windows/Linux browser is claimed here: the test asks real local Chrome
    to reject evidence declaring another input platform before any replay tab.
    """
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)
    cap.replayability = PinnedReplayability(random_seed=7)
    cap.bind_archivers([NetworkArchiver()])
    try:
        await cap.setup()
        actual = cap._input_platform
        assert actual in ("macos", "windows", "linux")
        other = "windows" if actual != "windows" else "linux"
        assert cap._session is not None and cap._replay_plan is not None
        fixture = RecordingScript(
            tmp_path,
            launch_flags=cap._session.metadata["launch_flags"],
            browser_defaults=cap._browser_defaults,
            input_platform=other,
        )
        fixture._plan = cap._replay_plan
        await fixture.exchange("http://shop.test/", body=b"<html>recorded fixture</html>")
        await fixture.commit(0)
        source = fixture.finish()
    finally:
        await cap.teardown()
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    replay = BrowserCapability()
    replay.backend = LocalChromeProvider(headless=True)
    replay.replayability = PinnedReplayability(random_seed=7, base_time="from_archive")
    replay.network_source = ArchiveNetworkSource()
    replay.bind_archivers([ReplayLogArchiver()])
    try:
        with pytest.raises(BrowserCooperationError, match="same actual platform"):
            await replay.setup(replay_archive=str(source))
        assert replay._input_platform == actual
        assert replay._recorder is None
    finally:
        await replay.teardown()
    assert before == {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    assert RunTrace(source).result is not None, "source remains readable for offline analysis"
