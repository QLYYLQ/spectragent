"""The browser's own client hints behind the pinned user agent (§4.14; ``client_hints.py``).

A user agent override without ``userAgentMetadata`` empties
``navigator.userAgentData`` and sends no ``Sec-CH-UA``, which no real Chrome
does. ``pinned`` therefore reads the browser's own client hints right after
launch (a temporary ``chrome://version`` tab) and its override carries them
whenever the identifier is in Chrome's own form; a Chrome identifier those
hints contradict (another version, platform or form) is refused at setup.
This module pins the parsing, the plan, the read and the setup on the fake
browser; the real Chrome, at record and at replay, is
``test_browser_client_hints_live.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser import capability as capability_module
from spectragent.core.capabilities.browser.client_hints import (
    CLIENT_HINTS_URL,
    ClientHints,
    chrome_identifier,
    identifier_problems,
    measure_client_hints,
)
from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
from spectragent.core.capabilities.browser.recorder import RecorderTarget
from spectragent.core.capabilities.browser.replay import NoneReplayability, PinnedReplayability
from tests._helpers.browser_archive import OwnedBrowser
from tests._helpers.fake_cdp import FAKE_CLIENT_HINTS, FakeBrowser

MAC_155 = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)
MAC_156 = MAC_155.replace("Chrome/155", "Chrome/156")
WINDOWS_155 = MAC_155.replace("Macintosh; Intel Mac OS X 10_15_7", "Windows NT 10.0; Win64; x64")
ANDROID_155 = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Mobile Safari/537.36"
)
#: Chrome 155's own hints under ``--user-agent`` (the fake browser answers these).
OWN = ClientHints.from_page(FAKE_CLIENT_HINTS)


def _target(kind: str) -> RecorderTarget:
    return RecorderTarget(session_id=f"s-{kind}", target_id=f"t-{kind}", type=kind)


def _overrides(plan: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The frame's and the worker's user agent override."""
    frame = {c.method: dict(c.params) for c in plan.target_commands(_target("page"))}
    worker = {c.method: dict(c.params) for c in plan.target_commands(_target("worker"))}
    return frame["Emulation.setUserAgentOverride"], worker["Network.setUserAgentOverride"]


# ── which identifiers are Chrome's own form ───────────────────────────


class TestChromeIdentifier:
    @pytest.mark.parametrize(
        ("user_agent", "major", "platform", "mobile"),
        [
            (MAC_155, "155", "macOS", False),
            (WINDOWS_155, "155", "Windows", False),
            (
                MAC_155.replace("Macintosh; Intel Mac OS X 10_15_7", "X11; Linux x86_64"),
                "155",
                "Linux",
                False,
            ),
            (ANDROID_155, "155", "Android", True),
            (MAC_155.replace("155.0.0.0", "155.0.8059.80"), "155", "macOS", False),
        ],
    )
    def test_chrome_s_own_form(
        self, user_agent: str, major: str, platform: str, mobile: bool
    ) -> None:
        claimed = chrome_identifier(user_agent)
        assert claimed is not None
        assert (claimed.major, claimed.platform, claimed.mobile) == (major, platform, mobile)

    @pytest.mark.parametrize(
        "user_agent",
        [
            None,
            "",
            "PinnedAgent/1.0 (fixture)",
            MAC_155.replace("Chrome/", "HeadlessChrome/"),
            f"{MAC_155} Edg/155.0.0.0",
            f"{MAC_155} OPR/120.0.0.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:128.0) Gecko/20100101 Firefox/128.0",
        ],
    )
    def test_any_other_string_is_not(self, user_agent: str | None) -> None:
        assert chrome_identifier(user_agent) is None


class TestClientHints:
    def test_read_values_round_trip_as_cdp_metadata(self) -> None:
        metadata = OWN.as_metadata()
        assert metadata["brands"] == FAKE_CLIENT_HINTS["brands"]
        assert (metadata["platform"], metadata["mobile"], metadata["fullVersion"]) == (
            "macOS",
            False,
            "",
        )
        assert ClientHints.from_record(metadata) == OWN
        assert ClientHints.from_record(None) is None

    def test_the_header_major_and_what_every_page_reads(self) -> None:
        assert OWN.major == "155"
        assert OWN.header() == '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"'
        assert OWN.low_entropy() == {
            "brands": FAKE_CLIENT_HINTS["brands"],
            "mobile": False,
            "platform": "macOS",
        }


class TestIdentifierProblems:
    def test_a_matching_identifier_has_none(self) -> None:
        assert identifier_problems(MAC_155, OWN) == []

    def test_another_version_names_both(self) -> None:
        (problem,) = identifier_problems(MAC_156, OWN)
        assert "Chrome 156" in problem and "Chrome 155" in problem and "↔" in problem

    def test_another_platform_and_form_are_named_at_once(self) -> None:
        problems = identifier_problems(ANDROID_155, OWN)
        assert len(problems) == 2
        assert "'Android'" in problems[0] and "'macOS'" in problems[0]
        assert "mobile" in problems[1]

    def test_a_string_in_another_form_has_none(self) -> None:
        assert identifier_problems("PinnedAgent/1.0 (fixture)", OWN) == []


# ── what the pinned override carries ──────────────────────────────────


class TestPinnedPlan:
    def test_pinned_needs_them_none_does_not(self) -> None:
        assert PinnedReplayability().needs_client_hints is True
        assert NoneReplayability().needs_client_hints is False

    def test_a_chrome_identifier_carries_the_browser_s_own(self) -> None:
        plan = PinnedReplayability(user_agent=MAC_155).plan(
            t0_ms=1, browser_user_agent="HeadlessChrome/155", client_hints=OWN
        )
        frame, worker = _overrides(plan)
        assert frame["userAgent"] == MAC_155
        assert frame["userAgentMetadata"] == worker["userAgentMetadata"] == OWN.as_metadata()
        assert plan.problems == ()
        assert plan.client_hints == OWN
        assert plan.fingerprint()["client_hints"] == OWN.as_metadata()

    def test_the_browser_s_own_identifier_carries_its_own(self) -> None:
        plan = PinnedReplayability(user_agent=None).plan(
            t0_ms=1, browser_user_agent="HeadlessChrome/155", client_hints=OWN
        )
        frame, _worker = _overrides(plan)
        assert frame["userAgent"] == "HeadlessChrome/155"
        assert frame["userAgentMetadata"] == OWN.as_metadata()
        assert plan.problems == ()

    @pytest.mark.parametrize("user_agent", [MAC_156, WINDOWS_155, ANDROID_155])
    def test_a_contradicted_chrome_identifier_is_a_problem(self, user_agent: str) -> None:
        plan = PinnedReplayability(user_agent=user_agent).plan(
            t0_ms=1, browser_user_agent="B", client_hints=OWN
        )
        frame, _worker = _overrides(plan)
        assert "userAgentMetadata" not in frame
        assert plan.client_hints is None
        assert plan.problems and all(user_agent in p for p in plan.problems)

    def test_an_identifier_in_another_form_carries_none(self) -> None:
        plan = PinnedReplayability(user_agent="PinnedAgent/1.0 (fixture)").plan(
            t0_ms=1, browser_user_agent="B", client_hints=OWN
        )
        frame, worker = _overrides(plan)
        assert "userAgentMetadata" not in frame and "userAgentMetadata" not in worker
        assert plan.problems == () and plan.fingerprint()["client_hints"] is None

    def test_hints_not_read_carry_none_and_are_no_problem(self) -> None:
        """The consistency check plans without reading the browser: parameters only."""
        plan = PinnedReplayability(user_agent=MAC_156).plan(t0_ms=1, browser_user_agent="B")
        frame, _worker = _overrides(plan)
        assert "userAgentMetadata" not in frame
        assert plan.problems == () and plan.client_hints is None


# ── the read, on the fake browser ─────────────────────────────────────


class TestTheRead:
    async def test_a_background_tab_read_and_closed(self) -> None:
        browser = FakeBrowser()
        client = browser.client("ws://fake", proxy=None)
        await client.start()
        hints = await measure_client_hints(client, timeout=1.0)
        assert hints == OWN
        created = [s for s in client.sent if s.method == "Target.createTarget"]
        assert [s.params for s in created] == [{"url": CLIENT_HINTS_URL, "background": True}]
        assert list(browser.targets) == ["page-1"], "the temporary tab is gone"

    async def test_a_page_that_never_reports_times_out_and_closes_the_tab(self) -> None:
        browser = FakeBrowser()
        browser.client_hints = None  # never a secure context: no userAgentData
        client = browser.client("ws://fake", proxy=None)
        await client.start()
        with pytest.raises(TimeoutError, match="no client hints"):
            await measure_client_hints(client, timeout=0.2)
        assert list(browser.targets) == ["page-1"]


# ── setup on the fake browser ─────────────────────────────────────────


async def _setup(
    tier: Any, browser: FakeBrowser | None = None
) -> tuple[BrowserCapability, FakeBrowser]:
    browser = browser or FakeBrowser()
    cap = BrowserCapability()
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap.replayability = tier
    cap._inject_cdp_factory(browser.client)
    return cap, browser


class TestSetup:
    async def test_the_recorder_prepares_tabs_with_the_browser_s_own_hints(self) -> None:
        cap, browser = await _setup(PinnedReplayability(user_agent=MAC_155))
        await cap.setup()
        try:
            overrides = [
                s.params
                for s in browser.recorder.sent
                if s.method == "Emulation.setUserAgentOverride"
            ]
            assert overrides, "the first tab was prepared"
            assert all(o["userAgentMetadata"] == OWN.as_metadata() for o in overrides)
            assert cap._replay_plan is not None and cap._replay_plan.client_hints == OWN
            # read before the recorder attached anything, the tab gone before the first is adopted
            methods = browser.recorder.methods()
            assert methods.index("Target.createTarget") < methods.index("Target.setAutoAttach")
            assert list(browser.targets) == ["page-1"]
        finally:
            await cap.teardown()

    async def test_a_contradicted_identifier_is_refused_before_any_tab_is_prepared(self) -> None:
        cap, browser = await _setup(PinnedReplayability(user_agent=MAC_156))
        with pytest.raises(BrowserCooperationError) as refused:
            await cap.setup()
        await cap.teardown()
        (problem,) = refused.value.problems
        assert "Chrome 156" in problem and "Chrome 155" in problem
        assert "Target.setAutoAttach" not in browser.recorder.methods()

    async def test_a_read_that_fails_refuses_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(capability_module, "_CLIENT_HINTS_TIMEOUT_S", 0.2)
        browser = FakeBrowser()
        browser.client_hints = None
        cap, _ = await _setup(PinnedReplayability(user_agent=MAC_155), browser)
        with pytest.raises(BrowserCooperationError, match="could not be read after launch"):
            await cap.setup()
        await cap.teardown()

    async def test_the_none_tier_reads_no_client_hints(self) -> None:
        cap, browser = await _setup(NoneReplayability())
        await cap.setup()
        try:
            assert cap._recorder is None
            assert not browser.client_hints_contexts
            # Actual input platform is now an independent requirement on every
            # browser, including none; it does not turn on UA emulation.
            assert browser.platform_contexts
        finally:
            await cap.teardown()
