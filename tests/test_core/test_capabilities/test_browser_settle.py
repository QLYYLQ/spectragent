"""The observation waits for the page to settle (``settle.py``, §4.14) — no Chrome.

Pinned here against fakes: the per-tab load tracker (``MainFrameLoad``) fed by
the control connection's ``Page.lifecycleEvent``; the wait itself
(``wait_for_settle``): a quiet page settles early, a busy page ends at the cap,
a page whose state cannot be read is captured at once, a document replaced
during the wait is waited for in its place; and the capability: the wait runs
before the one capture every fragment comes from, whichever fragments the run
negotiated, every fragment of the capture records it, and
``settle_max_wait_s: 0`` turns it off. The real-Chrome half is
``test_browser_settle_live.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.media import MEDIA_WORLD
from spectragent.core.capabilities.browser.navigation import (
    MainFrameLoad,
    MainFrameNavigation,
    NavigationFeed,
)
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.capabilities.browser.settle import (
    SETTLE_WORLD,
    PageSettle,
    wait_for_settle,
    watch_quiet,
)
from spectragent.core.trace.encoding import decode_fragment, encode_fragment
from tests._helpers.browser_archive import OwnedBrowser
from tests._helpers.fake_cdp import FakeBrowser
from tests._helpers.fragment_samples import PNG_BYTES

MAIN = "MAIN"


def _finished_without_suspending(coro: Any) -> Any:
    """Run *coro* one step: it must return without awaiting anything."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise AssertionError("the coroutine suspended")


# ── the load tracker: pure state ──────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TestMainFrameLoad:
    def test_a_document_never_heard_of_is_not_loading(self) -> None:
        load = MainFrameLoad(MAIN)
        assert (load.loader_id, load.loading) == (None, False)
        assert _finished_without_suspending(load.wait_loaded(10.0, budget_s=5.0)) is True

    def test_a_new_document_loads_at_its_load_event(self) -> None:
        load = MainFrameLoad(MAIN)
        load.lifecycle(MAIN, "L1", "init")
        assert load.loading
        load.lifecycle(MAIN, "L1", "DOMContentLoaded")
        assert load.loading
        load.lifecycle(MAIN, "L1", "load")
        assert not load.loading
        load.lifecycle(MAIN, "L2", "init")
        assert (load.loader_id, load.loading) == ("L2", True), "a new loader is a new document"

    def test_the_current_document_reported_again_counts(self) -> None:
        """Enabling lifecycle events makes Chrome report the current document again
        (``commit`` … ``load``, measured): a tab adopted after its load is loaded."""
        load = MainFrameLoad(MAIN)
        for name in ("commit", "DOMContentLoaded", "load", "networkAlmostIdle", "networkIdle"):
            load.lifecycle(MAIN, "L0", name)
        assert (load.loader_id, load.loading) == ("L0", False)

    def test_sub_frames_and_incomplete_events_are_ignored(self) -> None:
        load = MainFrameLoad(MAIN)
        load.lifecycle("CHILD", "C1", "init")
        load.lifecycle(None, "L1", "init")
        load.lifecycle(MAIN, None, "init")
        load.lifecycle(MAIN, "L1", None)
        assert load.loader_id is None
        load.lifecycle(MAIN, "L1", "init")
        load.lifecycle("CHILD", "L1", "load")
        assert load.loading, "a sub-frame's load is not the main document's"

    async def test_a_wait_returns_at_the_load(self) -> None:
        load = MainFrameLoad(MAIN)
        load.lifecycle(MAIN, "L1", "init")
        waiter = asyncio.ensure_future(load.wait_loaded(5.0, budget_s=5.0))
        await asyncio.sleep(0.02)
        assert not waiter.done()
        load.lifecycle(MAIN, "L1", "load")
        assert await asyncio.wait_for(waiter, 1.0) is True

    async def test_a_wait_gives_up_at_its_timeout(self) -> None:
        load = MainFrameLoad(MAIN)
        load.lifecycle(MAIN, "L1", "init")
        started = time.monotonic()
        assert await load.wait_loaded(0.05, budget_s=5.0) is False
        assert 0.04 <= time.monotonic() - started < 1.0

    async def test_a_document_is_waited_on_for_one_budget_in_total(self) -> None:
        """A document that never loads costs the budget once, not on every observation."""
        load = MainFrameLoad(MAIN)
        load.lifecycle(MAIN, "L1", "init")
        assert await load.wait_loaded(1.0, budget_s=0.05) is False
        # The budget is spent: the next observation does not wait on this document.
        assert _finished_without_suspending(load.wait_loaded(1.0, budget_s=0.05)) is True
        # A new document has a budget of its own.
        load.lifecycle(MAIN, "L2", "init")
        assert await load.wait_loaded(0.03, budget_s=0.05) is False

    async def test_a_document_replaced_mid_wait_is_waited_for_in_its_place(self) -> None:
        load = MainFrameLoad(MAIN)
        load.lifecycle(MAIN, "L1", "init")
        waiter = asyncio.ensure_future(load.wait_loaded(5.0, budget_s=5.0))
        await asyncio.sleep(0.01)
        load.lifecycle(MAIN, "L2", "init")  # a client-side redirect
        await asyncio.sleep(0.01)
        assert not waiter.done()
        load.lifecycle(MAIN, "L2", "load")
        assert await asyncio.wait_for(waiter, 1.0) is True

    def test_gone_settles_it(self) -> None:
        load = MainFrameLoad(MAIN)
        load.lifecycle(MAIN, "L1", "init")
        load.gone()
        assert not load.loading


class TestFeedFollowsTheLoad:
    def test_its_sessions_lifecycle_events_drive_the_load(self) -> None:
        client = FakeBrowser().client("ws://fake")
        feed = NavigationFeed(client, "S1", MainFrameNavigation(MAIN))
        assert feed.load.main_frame_id == MAIN
        client.fire(
            "Page.lifecycleEvent", {"frameId": MAIN, "loaderId": "L1", "name": "init"}, "S1"
        )
        client.fire(
            "Page.lifecycleEvent", {"frameId": MAIN, "loaderId": "L1", "name": "load"}, "S2"
        )
        assert feed.load.loading, "another session's load is not this tab's"
        client.fire(
            "Page.lifecycleEvent", {"frameId": MAIN, "loaderId": "L1", "name": "load"}, "S1"
        )
        assert not feed.load.loading
        client.fire(
            "Page.lifecycleEvent", {"frameId": MAIN, "loaderId": "L2", "name": "init"}, "S1"
        )
        client.fire("Target.detachedFromTarget", {"sessionId": "S1", "targetId": "T1"})
        assert not feed.load.loading, "a tab that is gone keeps nobody waiting"


# ── the wait: a scripted tab ──────────────────────────────────────────


class _Tab:
    """A tab's session as the settle wait sees it: the world, then the in-page probe.

    *probe* is called with the probe's budget (seconds) and returns its answer —
    a value, or an exception to raise — after sleeping as long as it likes.
    """

    def __init__(self, probe: Callable[[float], Any]) -> None:
        self.probe = probe
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.world_reply: Any = {"executionContextId": 7}

    @property
    def is_connected(self) -> bool:
        return True

    async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
        self.sent.append((method, dict(params or {})))
        if method == "Page.createIsolatedWorld":
            if isinstance(self.world_reply, Exception):
                raise self.world_reply
            return self.world_reply
        if method == "Runtime.evaluate":
            expression = str((params or {}).get("expression", ""))
            budget_ms = int(expression.rsplit(",", 1)[1].strip(" )\n"))
            answer = self.probe(budget_ms / 1000)
            if asyncio.iscoroutine(answer):
                answer = await answer
            if isinstance(answer, Exception):
                raise answer
            return answer
        return {}

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        return None

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        return None

    def probes(self) -> int:
        return sum(1 for method, _ in self.sent if method == "Runtime.evaluate")


def _probe_arguments(expression: str) -> tuple[int, int]:
    """The quiet window and the budget the probe was called with (milliseconds)."""
    quiet, budget = expression.rsplit("(", 1)[1].strip(" )\n").split(",")
    return int(quiet), int(budget)


def _quiet(waited_ms: int = 0) -> dict[str, Any]:
    value = {"quiet": True, "waited_ms": waited_ms, "mutations": 0, "resources": 0}
    return {"result": {"type": "object", "value": value}}


async def _busy(budget_s: float) -> dict[str, Any]:
    """The page kept changing: the probe resolves at its budget, not quiet."""
    await asyncio.sleep(budget_s)
    value = {"quiet": False, "waited_ms": int(budget_s * 1000), "mutations": 40, "resources": 3}
    return {"result": {"type": "object", "value": value}}


async def _settle(
    tab: _Tab,
    *,
    load: MainFrameLoad | None = None,
    navigation: MainFrameNavigation | None = None,
    frame_id: str | None = MAIN,
    quiet_ms: int = 500,
    max_wait_s: float = 5.0,
) -> PageSettle:
    return await wait_for_settle(
        tab,  # type: ignore[arg-type]
        session_id="S1",
        frame_id=frame_id,
        navigation=navigation,
        load=load,
        quiet_ms=quiet_ms,
        max_wait_s=max_wait_s,
    )


class TestWaitForSettle:
    async def test_a_quiet_page_settles_early(self) -> None:
        tab = _Tab(lambda _budget: _quiet(waited_ms=500))
        started = time.monotonic()
        settle = await _settle(tab, max_wait_s=5.0)
        assert (settle.outcome, settle.waiting_for, settle.detail) == ("settled", [], None)
        assert not settle.capped
        assert time.monotonic() - started < 1.0
        world, probe = tab.sent
        assert world == (
            "Page.createIsolatedWorld",
            {"frameId": MAIN, "worldName": SETTLE_WORLD},
        )
        assert probe[0] == "Runtime.evaluate"
        assert probe[1]["contextId"] == 7 and probe[1]["awaitPromise"] is True
        quiet_ms, budget_ms = _probe_arguments(probe[1]["expression"])
        assert quiet_ms == 500 and 4800 <= budget_ms <= 5000, (
            "the budget is what is left of the cap"
        )

    async def test_a_busy_page_ends_at_the_cap(self) -> None:
        tab = _Tab(_busy)
        started = time.monotonic()
        settle = await _settle(tab, max_wait_s=0.2)
        elapsed = time.monotonic() - started
        assert (settle.outcome, settle.waiting_for) == ("capped", ["dom"])
        assert settle.capped
        assert 0.15 <= elapsed < 1.0
        assert 150 <= settle.waited_ms < 1000

    async def test_a_page_that_never_answers_ends_at_the_cap(self) -> None:
        """A renderer too busy to run the probe (a page script spinning, a dialog)."""

        async def stuck(_budget: float) -> Any:
            await asyncio.Event().wait()

        tab = _Tab(stuck)
        started = time.monotonic()
        settle = await _settle(tab, max_wait_s=0.1)
        assert (settle.outcome, settle.waiting_for) == ("capped", ["dom"])
        # The probe's own budget plus the reply's grace, never longer.
        assert time.monotonic() - started < 2.0

    async def test_a_loading_document_is_waited_for_first(self) -> None:
        load = MainFrameLoad(MAIN)
        load.lifecycle(MAIN, "L1", "init")
        tab = _Tab(lambda _budget: _quiet())
        waiter = asyncio.ensure_future(_settle(tab, load=load, max_wait_s=5.0))
        await asyncio.sleep(0.05)
        assert not waiter.done() and tab.probes() == 0, "probed before the load"
        load.lifecycle(MAIN, "L1", "load")
        settle = await asyncio.wait_for(waiter, 2.0)
        assert settle.outcome == "settled" and tab.probes() == 1

    async def test_a_load_that_never_comes_ends_at_the_cap_once(self) -> None:
        load = MainFrameLoad(MAIN)
        load.lifecycle(MAIN, "L1", "init")
        tab = _Tab(lambda _budget: _quiet())
        first = await _settle(tab, load=load, max_wait_s=0.1)
        assert (first.outcome, first.waiting_for) == ("capped", ["load", "dom"])
        assert tab.probes() == 0
        # The same document is not waited on for its load again: the page is probed.
        second = await _settle(tab, load=load, max_wait_s=0.1)
        assert second.outcome == "settled" and tab.probes() == 1

    async def test_a_document_replaced_during_the_probe_is_waited_for(self) -> None:
        answers: list[Any] = [
            RuntimeError("CDP error: Execution context was destroyed."),
            _quiet(),
        ]
        tab = _Tab(lambda _budget: answers.pop(0))
        settle = await _settle(tab, navigation=MainFrameNavigation(MAIN))
        assert settle.outcome == "settled"
        assert tab.probes() == 2, "the new document was probed in its own world"

    @pytest.mark.parametrize(
        ("world", "probe", "detail"),
        [
            ({}, _quiet(), "the settle world has no execution context"),
            (RuntimeError("No frame for given id found"), _quiet(), "could not be created"),
            ({"executionContextId": 7}, {}, "returned no result"),
            (
                {"executionContextId": 7},
                {"result": {}, "exceptionDetails": {"text": "Uncaught"}},
                "raised in the page",
            ),
            ({"executionContextId": 7}, RuntimeError("CDP error: boom"), "probe failed"),
        ],
        ids=["no context", "world refused", "no result", "page exception", "command failed"],
    )
    async def test_an_unreadable_page_is_captured_at_once(
        self, world: Any, probe: Any, detail: str
    ) -> None:
        tab = _Tab(lambda _budget: probe)
        tab.world_reply = world
        started = time.monotonic()
        settle = await _settle(tab)
        assert settle.outcome == "unknown" and not settle.capped
        assert settle.detail is not None and detail in settle.detail
        assert time.monotonic() - started < 1.0

    async def test_an_unknown_main_frame_is_captured_at_once(self) -> None:
        tab = _Tab(lambda _budget: _quiet())
        settle = await _settle(tab, frame_id=None)
        assert settle.outcome == "unknown"
        assert settle.detail == "the tab's main frame is not known"
        assert tab.sent == []

    async def test_the_probe_reads_its_budget_from_what_is_left(self) -> None:
        budgets: list[float] = []

        def probe(budget: float) -> Any:
            budgets.append(budget)
            return _quiet()

        load = MainFrameLoad(MAIN)
        load.lifecycle(MAIN, "L1", "init")
        tab = _Tab(probe)
        waiter = asyncio.ensure_future(_settle(tab, load=load, max_wait_s=1.0))
        await asyncio.sleep(0.3)
        load.lifecycle(MAIN, "L1", "load")
        await asyncio.wait_for(waiter, 2.0)
        assert len(budgets) == 1 and 0.5 <= budgets[0] <= 0.75


class TestWatchQuiet:
    async def test_the_probe_runs_in_the_settle_world_and_waits_for_its_promise(self) -> None:
        tab = _Tab(lambda _budget: _quiet(waited_ms=512))
        watch = await watch_quiet(
            tab,  # type: ignore[arg-type]
            session_id="S1",
            frame_id=MAIN,
            quiet_ms=300,
            budget_s=2.5,
        )
        assert (watch.quiet, watch.waited_ms, watch.error) == (True, 512, None)
        _world, (_method, params) = tab.sent
        assert params["returnByValue"] is True
        expression = params["expression"]
        assert "MutationObserver" in expression and "PerformanceObserver" in expression
        quiet_ms, budget_ms = _probe_arguments(expression)
        assert quiet_ms == 300 and 2400 <= budget_ms <= 2500


# ── the capability: one wait per capture, recorded on every fragment ──


async def _capability(browser: FakeBrowser, *fragments: str, **knobs: Any) -> BrowserCapability:
    cap = BrowserCapability(**knobs)
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    await cap.setup()
    cap.apply_negotiation(set(fragments))
    return cap


def _settle_methods(browser: FakeBrowser, sid: str) -> list[str]:
    """The control connection's commands on *sid* that belong to a capture, in order."""
    wanted = ("Page.createIsolatedWorld", "Runtime.evaluate", "Page.captureScreenshot")
    return [
        sent.method
        if sent.method != "Page.createIsolatedWorld"
        else f"{sent.method}:{sent.params.get('worldName')}"
        for sent in browser.control.sent
        if sent.session_id == sid and sent.method in wanted
    ]


class TestCapabilitySettles:
    async def test_the_wait_comes_before_the_capture(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser, "browser.screenshot")
        try:
            sid = cap._default_session_id
            assert sid is not None
            await cap.begin_observation(1)
            shot = await asyncio.wait_for(cap.resolve_fragment("browser.screenshot"), 5.0)
            assert isinstance(shot, ScreenshotObs)
            # The media reads around the screenshot come after the wait too (media.py).
            assert _settle_methods(browser, sid) == [
                f"Page.createIsolatedWorld:{SETTLE_WORLD}",
                "Runtime.evaluate",
                f"Page.createIsolatedWorld:{MEDIA_WORLD}",
                "Runtime.evaluate",
                "Page.captureScreenshot",
                f"Page.createIsolatedWorld:{MEDIA_WORLD}",
                "Runtime.evaluate",
            ]
            assert shot.settle is not None and shot.settle.outcome == "settled"
            assert browser.settle_probes == 1
        finally:
            await cap.teardown()

    async def test_every_fragment_of_the_capture_records_the_same_wait(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(
            browser, "browser.dom", "browser.screenshot", "browser.browser_meta"
        )
        try:
            await cap.begin_observation(1)
            dom = await cap.resolve_fragment("browser.dom")
            shot = await cap.resolve_fragment("browser.screenshot")
            meta = await cap.resolve_fragment("browser.browser_meta")
            assert isinstance(dom, DOMObs)
            assert isinstance(shot, ScreenshotObs)
            assert isinstance(meta, BrowserMetaObs)
            assert dom.settle is not None
            assert dom.settle == shot.settle == meta.settle
            assert browser.settle_probes == 1, "one wait per capture, not one per fragment"
        finally:
            await cap.teardown()

    async def test_a_dom_only_observation_waits_the_same(self) -> None:
        """Not every agent looks at screenshots: the wait is the observation's, not the image's."""
        browser = FakeBrowser()
        cap = await _capability(browser, "browser.dom")
        try:
            await cap.begin_observation(1)
            dom = await cap.resolve_fragment("browser.dom")
            assert isinstance(dom, DOMObs)
            assert dom.settle is not None and dom.settle.outcome == "settled"
            assert browser.settle_probes == 1
            assert "Page.captureScreenshot" not in browser.control.methods()
        finally:
            await cap.teardown()

    async def test_a_capped_wait_still_captures_and_says_so(self) -> None:
        browser = _HeldProbe()
        cap = await _capability(
            browser, "browser.screenshot", settle_quiet_ms=100, settle_max_wait_s=0.2
        )
        try:
            await cap.begin_observation(1)
            started = time.monotonic()
            shot = await asyncio.wait_for(cap.resolve_fragment("browser.screenshot"), 5.0)
            assert isinstance(shot, ScreenshotObs) and shot.image_bytes == browser.screenshot
            assert shot.settle is not None
            assert (shot.settle.outcome, shot.settle.waiting_for) == ("capped", ["dom"])
            assert 0.15 <= time.monotonic() - started < 2.0
        finally:
            await cap.teardown()

    async def test_zero_turns_the_wait_off(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser, "browser.dom", "browser.screenshot", settle_max_wait_s=0)
        try:
            await cap.begin_observation(1)
            dom = await cap.resolve_fragment("browser.dom")
            shot = await cap.resolve_fragment("browser.screenshot")
            assert isinstance(dom, DOMObs) and isinstance(shot, ScreenshotObs)
            assert dom.settle is None and shot.settle is None
            assert browser.settle_probes == 0
            assert not browser.settle_contexts, "no settle world was created"
        finally:
            await cap.teardown()

    async def test_each_attempt_waits_and_the_served_capture_records_its_own(self) -> None:
        browser = _RefusesFirstCapture()
        cap = await _capability(browser, "browser.screenshot")
        try:
            await cap.begin_observation(1)
            shot = await asyncio.wait_for(cap.resolve_fragment("browser.screenshot"), 5.0)
            assert isinstance(shot, ScreenshotObs)
            assert browser.captures == 2 and browser.settle_probes == 2
            assert shot.settle is not None and shot.settle.outcome == "settled"
        finally:
            await cap.teardown()

    async def test_attach_enables_lifecycle_events_after_the_feed_listens(self) -> None:
        """Chrome reports the current document's lifecycle again when the events are
        enabled; the feed must already be listening, or a tab adopted after its load
        is unknown to the wait."""
        browser = FakeBrowser()
        cap = await _capability(browser, "browser.screenshot")
        try:
            sid = cap._default_session_id
            methods = browser.control.methods(session_id=sid)
            assert methods.index("Page.getFrameTree") < methods.index(
                "Page.setLifecycleEventsEnabled"
            )
            assert sid is not None and sid in cap._navigation_feeds
        finally:
            await cap.teardown()


class _HeldProbe(FakeBrowser):
    """A page that keeps changing: the settle probe resolves at its budget, not quiet."""

    async def handle(
        self, client: Any, method: str, params: dict[str, Any], session_id: Any
    ) -> Any:
        if method == "Runtime.evaluate" and params.get("contextId") in self.settle_contexts:
            expression = str(params.get("expression", ""))
            budget_ms = int(expression.rsplit(",", 1)[1].strip(" )\n"))
            return await _busy(budget_ms / 1000)
        return await super().handle(client, method, params, session_id)


class _RefusesFirstCapture(FakeBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.captures = 0

    async def handle(
        self, client: Any, method: str, params: dict[str, Any], session_id: Any
    ) -> Any:
        if method == "Page.captureScreenshot":
            self.captures += 1
            if self.captures == 1:
                raise RuntimeError("CDP error: Not attached to an active page")
        return await super().handle(client, method, params, session_id)


class TestTraceRecord:
    """Every browser fragment carries the record in the trace, and reads it back."""

    @pytest.mark.parametrize(
        "fragment",
        [
            DOMObs(),
            ScreenshotObs(image_bytes=PNG_BYTES, viewport_width=1280, viewport_height=720),
            BrowserMetaObs(url="http://shop.test/"),
        ],
        ids=["dom", "screenshot", "browser_meta"],
    )
    def test_a_record_round_trips(self, fragment: Any) -> None:
        record = PageSettle(waited_ms=5003, outcome="capped", waiting_for=["dom"])
        fragment.settle = record
        encoded = encode_fragment(3, fragment)
        assert encoded is not None
        payload = json.loads(encoded.file.data)
        assert payload["settle"] == {
            "waited_ms": 5003,
            "outcome": "capped",
            "waiting_for": ["dom"],
            "detail": None,
        }
        artifact = encoded.artifact.data if encoded.artifact is not None else None
        back = decode_fragment(encoded.key, type(fragment), payload, artifact)
        assert back.settle == record  # type: ignore[attr-defined]

    def test_no_record_writes_no_key(self) -> None:
        """A fragment no waiting observation served writes what it always wrote."""
        encoded = encode_fragment(0, BrowserMetaObs(url="http://shop.test/"))
        assert encoded is not None
        payload = json.loads(encoded.file.data)
        assert "settle" not in payload
        back = decode_fragment(encoded.key, BrowserMetaObs, payload, None)
        assert back.settle is None  # type: ignore[attr-defined]


def _compose(**capability_block: Any) -> BrowserCapability:
    """Build a BrowserCapability the real way — through the Composer."""
    import spectragent.core.capabilities.browser.backends.local_chrome  # noqa: F401
    from spectragent.core.composer import ConfigComposer
    from spectragent.core.registries import collect_registrars

    run = ConfigComposer(registrars=collect_registrars()).compose(
        {
            "capability": [
                {
                    "capability_id": "browser",
                    "mouse_trajectory": {"trajectory_type": "instant"},
                    "typing_rhythm": {"rhythm_type": "instant"},
                    "replayability": {"replayability_id": "none"},
                    "network_source": {"source_id": "live"},
                    "dom_cleaner": {"cleaner_type": "browser_use"},
                    "dom_serializer": {"serializer_type": "text"},
                    "backend": {"provider_id": "local_chrome"},
                    **capability_block,
                }
            ]
        }
    )
    return run.single("capability")


class TestKnobs:
    def test_defaults(self) -> None:
        cap = BrowserCapability()
        assert (cap.settle_quiet_ms, cap.settle_max_wait_s) == (500, 5.0)
        composed = _compose()
        assert (composed.settle_quiet_ms, composed.settle_max_wait_s) == (500, 5.0)

    def test_yaml_reaches_both_knobs(self) -> None:
        cap = _compose(settle_quiet_ms=250, settle_max_wait_s=2.5)
        assert (cap.settle_quiet_ms, cap.settle_max_wait_s) == (250, 2.5)
        assert _compose(settle_max_wait_s=0).settle_max_wait_s == 0

    @pytest.mark.parametrize(
        "block",
        [
            {"settle_quiet_ms": -1},
            {"settle_max_wait_s": -0.5},
            {"settle_max_wait_s": 500},  # milliseconds written where seconds belong
            {"settle_max_wait_s": None},
        ],
        ids=["negative window", "negative cap", "a cap in milliseconds", "null"],
    )
    def test_nonsense_fails_validation(self, block: dict[str, Any]) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match=next(iter(block))):
            _compose(**block)

    def test_a_window_longer_than_the_cap_is_refused(self) -> None:
        """It could never be observed before the cap: every observation would wait the cap."""
        from spectragent.core.composer import InstantiationError

        with pytest.raises(ValueError, match="settle_quiet_ms .* is longer than settle_max_wait_s"):
            BrowserCapability(settle_quiet_ms=3000, settle_max_wait_s=2)
        with pytest.raises(InstantiationError, match="settle_quiet_ms .* is longer than"):
            _compose(settle_quiet_ms=3000, settle_max_wait_s=2)

    def test_a_turned_off_wait_takes_any_window(self) -> None:
        cap = BrowserCapability(settle_quiet_ms=3000, settle_max_wait_s=0)
        assert cap.settle_max_wait_s == 0
