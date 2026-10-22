"""The browser's ``capture_state`` fragment and its resolver — no Chrome.

``docs/architecture_design.md`` §4.4, "跨平台采集状态 ``capture_state``".
Pinned here:

- the fragment: settle and foreground change are projections of the
  capture's own ``settle`` / ``focus_moved`` records, read when asked (the
  capability assigns them after the resolvers answered), and its trace round
  trip gives every fact back;
- the resolver (``CaptureStateWatchdog``) against a scripted tab: one probe in
  its own isolated world, a measured count with short names, ``unknown`` with
  a reason whenever the probe fails, runs out or answers nonsense — never a
  raise and never ``0``; the pointer is the tab's last confirmed position,
  whether or not the screenshot draws it; a capture that did not negotiate the
  fragment costs nothing;
- the capability on the fake browser: the fragment comes from the same
  capture as the screenshot, with the same settle record and focus move, and a
  failing or hung probe leaves the screenshot served by a single capture.

The real-Chrome half is ``test_browser_capture_state_live.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.input.mouse import PointerPosition
from spectragent.core.capabilities.browser.observations import (
    BrowserCaptureFragment,
    BrowserCaptureStateObs,
    BrowserMetaObs,
    ScreenshotObs,
)
from spectragent.core.capabilities.browser.profiles import BrowserProfileConfig
from spectragent.core.capabilities.browser.settle import PageSettle
from spectragent.core.capabilities.browser.tabs import FocusMove
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.base import WatchdogContext
from spectragent.core.capabilities.browser.watchdogs.capture_state_watchdog import (
    CAPTURE_STATE_WORLD,
    IDENTIFIER_MAX_CHARS,
    CaptureStateWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.events import BrowserStateRequestEvent
from spectragent.core.trace.encoding import decode_fragment, encode_fragment
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import ObservationRegistrar
from spectragent.schemas.observation.capture_state import (
    MAX_RESOURCE_IDENTIFIERS,
    CaptureStateFragment,
    FailedVisibleResources,
    ForegroundChange,
    PointerState,
    SettleState,
)
from tests._helpers.browser_archive import OwnedBrowser
from tests._helpers.fake_cdp import FakeBrowser

ASKED = BrowserStateRequestEvent(fragments=frozenset({"browser.capture_state"}))
UNKNOWN_POINTER = PointerState(status="unknown")
NOT_MEASURED = FailedVisibleResources(status="unknown", detail="not measured")


def _state(**fields: Any) -> BrowserCaptureStateObs:
    return BrowserCaptureStateObs(
        pointer=fields.pop("pointer", UNKNOWN_POINTER),
        failed_visible_resources=fields.pop("failed_visible_resources", NOT_MEASURED),
        **fields,
    )


# ── the fragment ──────────────────────────────────────────────────────


class TestTheFragment:
    def test_it_is_a_browser_capture_and_a_capture_state(self) -> None:
        assert ObservationRegistrar.get("browser.capture_state") is BrowserCaptureStateObs
        fragment = _state()
        assert isinstance(fragment, BrowserCaptureFragment)
        assert isinstance(fragment, CaptureStateFragment)
        assert fragment.fragment_id == "browser.capture_state"
        assert BrowserCaptureStateObs.__source__ == "env"

    @pytest.mark.parametrize(
        ("settle", "projected"),
        [
            (None, None),
            (
                PageSettle(waited_ms=640, outcome="settled"),
                SettleState(outcome="settled", waited_ms=640),
            ),
            (
                PageSettle(
                    waited_ms=5000, outcome="capped", waiting_for=["load", "dom", "content"]
                ),
                SettleState(outcome="capped", waited_ms=5000, pending=("load", "dom", "content")),
            ),
            (
                PageSettle(waited_ms=5000, outcome="capped", waiting_for=["content"]),
                SettleState(outcome="capped", waited_ms=5000, pending=("content",)),
            ),
            (
                PageSettle(
                    waited_ms=3, outcome="unknown", detail="the tab's main frame is not known"
                ),
                SettleState(
                    outcome="unknown", waited_ms=3, detail="the tab's main frame is not known"
                ),
            ),
        ],
        ids=["off", "settled", "capped", "capped on an empty viewport", "unknown"],
    )
    def test_settle_is_the_captures_settle_record(
        self, settle: PageSettle | None, projected: SettleState | None
    ) -> None:
        assert _state(settle=settle).settle_state == projected

    @pytest.mark.parametrize(
        ("move", "projected"),
        [
            (None, None),
            (
                FocusMove(reason="new_tab", from_tab="T1", to_tab="T2"),
                ForegroundChange(reason="opened", from_surface="T1", to_surface="T2"),
            ),
            (
                FocusMove(reason="tab_closed", from_tab="T2", to_tab="T1"),
                ForegroundChange(reason="closed", from_surface="T2", to_surface="T1"),
            ),
        ],
        ids=["stayed", "new-tab", "tab-closed"],
    )
    def test_foreground_change_is_the_captures_focus_move(
        self, move: FocusMove | None, projected: ForegroundChange | None
    ) -> None:
        assert _state(focus_moved=move).foreground_change == projected

    def test_the_records_assigned_after_the_resolver_answered_are_the_ones_projected(self) -> None:
        """The capability assigns ``settle`` / ``focus_moved`` after the watchdogs answered."""
        fragment = _state()
        assert fragment.settle_state is None and fragment.foreground_change is None
        fragment.settle = PageSettle(waited_ms=120, outcome="settled")
        fragment.focus_moved = FocusMove(reason="new_tab", from_tab="A", to_tab="B")
        assert fragment.settle_state == SettleState(outcome="settled", waited_ms=120)
        assert fragment.foreground_change == ForegroundChange(
            reason="opened", from_surface="A", to_surface="B"
        )

    def test_the_trace_gives_every_fact_back(self) -> None:
        fragment = _state(
            settle=PageSettle(waited_ms=5000, outcome="capped", waiting_for=["dom"]),
            focus_moved=FocusMove(reason="tab_closed", from_tab="T2", to_tab="T1"),
            pointer=PointerState(status="known", x=120, y=95),
            failed_visible_resources=FailedVisibleResources(
                status="measured", count=2, identifiers=("missing.png", "hero.webp")
            ),
        )
        encoded = encode_fragment(4, fragment)
        assert encoded is not None and encoded.artifact is None
        assert encoded.file.rel_path.endswith("004_browser.capture_state.json")
        payload = json.loads(encoded.file.data)
        # Only the records: the projections are derived from them, never stored twice.
        assert "settle_state" not in payload and "foreground_change" not in payload
        assert payload["failed_visible_resources"]["identifiers"] == ["missing.png", "hero.webp"]
        back = decode_fragment(encoded.key, BrowserCaptureStateObs, payload, None)
        assert isinstance(back, BrowserCaptureStateObs)
        assert back.settle_state == fragment.settle_state
        assert back.foreground_change == fragment.foreground_change
        assert back.pointer == fragment.pointer
        assert back.failed_visible_resources == fragment.failed_visible_resources
        assert back.captured_at == fragment.captured_at

    def test_no_record_writes_no_record_key(self) -> None:
        encoded = encode_fragment(0, _state())
        assert encoded is not None
        payload = json.loads(encoded.file.data)
        assert "settle" not in payload and "focus_moved" not in payload
        back = decode_fragment(encoded.key, BrowserCaptureStateObs, payload, None)
        assert isinstance(back, BrowserCaptureStateObs)
        assert back.settle_state is None and back.foreground_change is None
        assert back.failed_visible_resources == NOT_MEASURED

    def test_summary(self) -> None:
        fragment = _state(
            settle=PageSettle(waited_ms=5000, outcome="capped", waiting_for=["dom"]),
            focus_moved=FocusMove(reason="new_tab", from_tab="T1", to_tab="T2"),
            pointer=PointerState(status="known", x=120, y=95),
            failed_visible_resources=FailedVisibleResources(status="measured", count=1),
        )
        assert fragment.summary() == (
            "capped 5000ms; foreground opened; pointer (120, 95); 1 failed visible resource"
        )


# ── the resolver against a scripted tab ───────────────────────────────


class _Tab:
    """The tab's session as the resolver (and the input controller) sees it.

    ``reply`` is ``Runtime.evaluate``'s whole answer; ``raises`` makes it fail;
    ``hold`` makes it never answer. ``world`` is ``Page.createIsolatedWorld``'s.
    """

    is_connected = True

    def __init__(self, value: Any = None, **script: Any) -> None:
        self.reply: Any = script.get("reply", {"result": {"type": "object", "value": value}})
        self.raises: BaseException | None = script.get("raises")
        self.hold: bool = script.get("hold", False)
        self.world: Any = script.get("world", {"executionContextId": 41})
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def on_event(self, method: str, callback: Any) -> None:
        return None

    def off_event(self, method: str, callback: Any) -> None:
        return None

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append((method, dict(params or {}), session_id))
        if method == "Page.createIsolatedWorld":
            if isinstance(self.world, BaseException):
                raise self.world
            return self.world
        if method == "Runtime.evaluate":
            if self.hold:
                await asyncio.Event().wait()
            if self.raises is not None:
                raise self.raises
            return self.reply
        return {}

    def methods(self) -> list[str]:
        return [method for method, _params, _sid in self.calls]


def _context(tab: _Tab, **fields: Any) -> WatchdogContext:
    fields.setdefault("main_frame_id_getter", lambda: "MAIN")
    return WatchdogContext(cdp=tab, profile=BrowserProfileConfig(), **fields)  # type: ignore[arg-type]


async def _resolve(tab: _Tab, **fields: Any) -> BrowserCaptureStateObs:
    watchdog = CaptureStateWatchdog.build(_context(tab, **fields))
    watchdog._session_id = "tab"
    summary = await watchdog._on_browser_state_request(ASKED)
    assert summary is not None and summary.document is None
    assert set(summary.fragments) == {"browser.capture_state"}
    fragment = summary.fragments["browser.capture_state"]
    assert isinstance(fragment, BrowserCaptureStateObs)
    return fragment


class TestFailedImages:
    async def test_the_probe_runs_in_its_own_world_and_counts_what_failed(self) -> None:
        tab = _Tab({"count": 1, "names": ["missing.png"]})
        state = await _resolve(tab)
        assert state.failed_visible_resources == FailedVisibleResources(
            status="measured", count=1, identifiers=("missing.png",)
        )
        world, evaluate = tab.calls
        assert world == (
            "Page.createIsolatedWorld",
            {"frameId": "MAIN", "worldName": CAPTURE_STATE_WORLD},
            "tab",
        )
        method, params, session = evaluate
        assert (method, session) == ("Runtime.evaluate", "tab")
        assert params["contextId"] == 41 and params["returnByValue"] is True
        assert "awaitPromise" not in params
        expression = params["expression"]
        for rule in ("img.complete", "naturalWidth !== 0", "checkVisibility", "currentSrc"):
            assert rule in expression

    async def test_none_failed_is_a_measured_zero(self) -> None:
        state = await _resolve(_Tab({"count": 0, "names": []}))
        assert state.failed_visible_resources == FailedVisibleResources(status="measured", count=0)

    async def test_names_are_bounded_and_clipped_while_the_count_stays_whole(self) -> None:
        long = "a-very-long-image-file-name-from-a-content-delivery-network.jpeg"
        names = [long, *(f"{n}.png" for n in range(20))]
        state = await _resolve(_Tab({"count": 21, "names": names}))
        failed = state.failed_visible_resources
        assert failed.count == 21
        assert len(failed.identifiers) == MAX_RESOURCE_IDENTIFIERS
        assert failed.identifiers[0] == long[: IDENTIFIER_MAX_CHARS - 1] + "…"
        assert len(failed.identifiers[0]) == IDENTIFIER_MAX_CHARS
        assert failed.identifiers[1:] == tuple(f"{n}.png" for n in range(7))

    async def test_never_more_names_than_failures(self) -> None:
        state = await _resolve(_Tab({"count": 1, "names": ["a.png", "b.png"]}))
        assert state.failed_visible_resources.identifiers == ("a.png",)

    @pytest.mark.parametrize(
        ("tab", "detail"),
        [
            (_Tab(raises=RuntimeError("Execution context was destroyed.")), "the probe failed: "),
            (
                _Tab(reply={"exceptionDetails": {"text": "Uncaught"}, "result": {}}),
                "the probe raised in the page",
            ),
            (_Tab(None), "the probe returned no result"),
            (_Tab(reply=None), "the probe returned no result"),
            (_Tab({"count": -1, "names": []}), "the probe returned no result"),
            (_Tab({"count": True, "names": []}), "the probe returned no result"),
            (_Tab({"count": 2, "names": "a.png"}), "the probe returned no result"),
            (_Tab({"count": 2, "names": [3]}), "the probe returned no result"),
            (_Tab(world={}), "the probe's world has no execution context"),
            (_Tab(world=RuntimeError("No frame for given id found")), "the probe failed: "),
        ],
        ids=[
            "evaluate-fails",
            "page-exception",
            "no-value",
            "no-reply",
            "negative",
            "boolean",
            "names-not-a-list",
            "names-not-text",
            "no-context",
            "world-fails",
        ],
    )
    async def test_a_failed_measurement_is_unknown_with_its_reason(
        self, tab: _Tab, detail: str
    ) -> None:
        failed = (await _resolve(tab)).failed_visible_resources
        assert failed.status == "unknown" and failed.count is None and not failed.identifiers
        assert failed.detail is not None and failed.detail.startswith(detail)
        assert len(failed.detail) <= 160

    async def test_a_probe_that_does_not_answer_within_the_budget_is_unknown(self) -> None:
        started = time.monotonic()
        state = await _resolve(_Tab(hold=True), capture_state_timeout_s=0.05)
        assert time.monotonic() - started < 1.0
        assert state.failed_visible_resources == FailedVisibleResources(
            status="unknown", detail="the probe got no answer within 0.05s"
        )

    async def test_an_unknown_main_frame_is_unknown_without_a_command(self) -> None:
        tab = _Tab({"count": 0, "names": []})
        state = await _resolve(tab, main_frame_id_getter=lambda: None)
        assert state.failed_visible_resources == FailedVisibleResources(
            status="unknown", detail="the tab's main frame is not known"
        )
        assert tab.calls == []

    async def test_a_capture_that_did_not_negotiate_it_costs_nothing(self) -> None:
        tab = _Tab({"count": 0, "names": []})
        watchdog = CaptureStateWatchdog.build(_context(tab))
        other = BrowserStateRequestEvent(fragments=frozenset({"browser.screenshot"}))
        assert await watchdog._on_browser_state_request(other) is None
        assert tab.calls == []


class TestPointer:
    @pytest.mark.parametrize("cursor", [False, True], ids=["cursor-off", "cursor-on"])
    async def test_the_tabs_last_confirmed_pointer(self, cursor: bool) -> None:
        """Read from the tab's ``PointerPosition``, which the input controller moves."""
        tab = _Tab({"count": 0, "names": []})
        ctx = _context(tab, input_platform="macos", screenshot_cursor=cursor)
        action = ActionWatchdog.build(ctx)
        resolver = CaptureStateWatchdog.build(ctx)
        before = await resolver._on_browser_state_request(ASKED)
        assert before is not None
        assert before.fragments["browser.capture_state"].pointer == UNKNOWN_POINTER
        await action._mouse.move_to(80, 70)
        after = await resolver._on_browser_state_request(ASKED)
        assert after is not None
        assert after.fragments["browser.capture_state"].pointer == PointerState(
            status="known", x=80, y=70
        )

    async def test_an_unconfirmed_move_makes_it_unknown_again(self) -> None:
        position = PointerPosition(position=(10, 20))
        state = await _resolve(_Tab({"count": 0, "names": []}), pointer_position=position)
        assert state.pointer == PointerState(status="known", x=10, y=20)
        position.position = None  # what the controller does when a move is not confirmed
        assert (
            await _resolve(_Tab({"count": 0, "names": []}), pointer_position=position)
        ).pointer == (UNKNOWN_POINTER)

    async def test_another_tab_has_its_own_pointer(self) -> None:
        """Each tab's watchdogs are built from that tab's context, with its own position."""
        moved, other = _Tab({"count": 0, "names": []}), _Tab({"count": 0, "names": []})
        moved_ctx = _context(moved, input_platform="macos")
        await ActionWatchdog.build(moved_ctx)._mouse.move_to(30, 40)
        assert (await _resolve(moved, pointer_position=moved_ctx.pointer_position)).pointer == (
            PointerState(status="known", x=30, y=40)
        )
        assert (await _resolve(other)).pointer == UNKNOWN_POINTER


# ── the capability: one capture, the same records, never a failed slice ──


class _ProbedBrowser(FakeBrowser):
    """The fake browser, answering the capture-state probe as *mode* says."""

    def __init__(self, mode: str = "answer") -> None:
        super().__init__()
        self.mode = mode
        self.answer: dict[str, Any] = {"count": 1, "names": ["missing.png"]}
        self.contexts: dict[int, str | None] = {}
        self.probes: list[str | None] = []
        self.captures = 0

    async def handle(
        self, client: Any, method: str, params: dict[str, Any], session_id: Any
    ) -> Any:
        if method == "Page.createIsolatedWorld" and params.get("worldName") == CAPTURE_STATE_WORLD:
            context = 21000 + len(self.contexts) + 1
            self.contexts[context] = session_id
            return {"executionContextId": context}
        if method == "Runtime.evaluate" and params.get("contextId") in self.contexts:
            self.probes.append(session_id)
            if self.mode == "fail":
                raise RuntimeError("CDP error: Execution context was destroyed.")
            if self.mode == "hold":
                await asyncio.Event().wait()
            return {"result": {"type": "object", "value": dict(self.answer)}}
        if method == "Page.captureScreenshot":
            self.captures += 1
        return await super().handle(client, method, params, session_id)


FRAGMENTS = ("browser.browser_meta", "browser.capture_state", "browser.screenshot")


async def _capability(browser: FakeBrowser, *fragments: str, **knobs: Any) -> BrowserCapability:
    cap = BrowserCapability(**knobs)
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    await cap.setup()
    cap.apply_negotiation(set(fragments or FRAGMENTS))
    return cap


async def _observe(cap: BrowserCapability, cycle: int) -> dict[str, Any]:
    await cap.begin_observation(cycle)
    return {
        key: await asyncio.wait_for(cap.resolve_fragment(key), 5.0)
        for key in sorted(cap.provided_fragments)
    }


class TestTheCapture:
    async def test_it_comes_from_the_screenshots_capture_with_the_same_records(self) -> None:
        browser = _ProbedBrowser()
        cap = await _capability(browser)
        try:
            observed = await _observe(cap, 1)
            state, shot = observed["browser.capture_state"], observed["browser.screenshot"]
            assert isinstance(state, BrowserCaptureStateObs) and isinstance(shot, ScreenshotObs)
            settle = shot.settle
            assert settle is not None and state.settle == settle
            assert state.settle_state == SettleState(
                outcome=settle.outcome, waited_ms=settle.waited_ms
            )
            assert state.foreground_change is None and state.focus_moved is None
            assert state.pointer == UNKNOWN_POINTER
            assert state.failed_visible_resources == FailedVisibleResources(
                status="measured", count=1, identifiers=("missing.png",)
            )
            assert browser.captures == 1 and browser.probes == [cap.default_session_id]
            # Read twice in one observation: the same capture, no second probe.
            assert await cap.resolve_fragment("browser.capture_state") is state
            assert len(browser.probes) == 1
        finally:
            await cap.teardown()

    async def test_it_probes_after_the_screenshot_and_before_browser_meta(self) -> None:
        browser = _ProbedBrowser()
        cap = await _capability(browser)
        try:
            await _observe(cap, 1)
            sid = cap.default_session_id
            order = [
                s.method
                if s.method != "Page.createIsolatedWorld"
                else f"world:{s.params.get('worldName')}"
                for s in browser.control.sent
                if s.session_id == sid
                and (
                    s.method in ("Page.captureScreenshot", "Page.getNavigationHistory")
                    or s.params.get("worldName") == CAPTURE_STATE_WORLD
                )
            ]
            assert order == [
                "Page.captureScreenshot",
                f"world:{CAPTURE_STATE_WORLD}",
                "Page.getNavigationHistory",
            ]
        finally:
            await cap.teardown()

    @pytest.mark.parametrize("mode", ["fail", "hold"])
    async def test_a_probe_that_fails_never_fails_the_capture(self, mode: str) -> None:
        """A missing negotiated fragment would fail every browser fragment of the
        observation, the screenshot included: the probe's trouble stays inside it."""
        browser = _ProbedBrowser(mode)
        cap = await _capability(browser, capture_state_timeout_s=0.1)
        try:
            started = time.monotonic()
            observed = await _observe(cap, 1)
            assert time.monotonic() - started < 3.0
            shot, state = observed["browser.screenshot"], observed["browser.capture_state"]
            assert isinstance(shot, ScreenshotObs) and shot.image_bytes == browser.screenshot
            assert isinstance(state, BrowserCaptureStateObs)
            failed = state.failed_visible_resources
            assert failed.status == "unknown" and failed.count is None
            assert failed.detail is not None
            assert ("no answer within 0.1s" in failed.detail) == (mode == "hold")
            assert browser.captures == 1, "the capture was not re-fired"
            assert state.settle == shot.settle
        finally:
            await cap.teardown()

    async def test_not_negotiated_nothing_is_probed(self) -> None:
        browser = _ProbedBrowser()
        cap = await _capability(browser, "browser.screenshot", "browser.browser_meta")
        try:
            observed = await _observe(cap, 1)
            assert set(observed) == {"browser.screenshot", "browser.browser_meta"}
            assert browser.contexts == {} and browser.probes == []
        finally:
            await cap.teardown()

    async def test_the_tab_the_capture_followed_is_its_foreground_change(self) -> None:
        browser = _ProbedBrowser()
        cap = await _capability(browser)
        try:
            first = await _observe(cap, 1)
            assert first["browser.capture_state"].foreground_change is None
            opener = cap._session_to_target[cap.default_session_id]

            async def opens(
                _params: dict[str, Any], *, capability: BrowserCapability
            ) -> ActionResult:
                session = capability._target_to_session[opener]
                browser.control.fire(
                    "Page.windowOpen",
                    {"url": "https://shop.test/two", "userGesture": True},
                    session,
                )
                browser.open_target("page-2", "page", url="https://shop.test/two", opener_id=opener)
                return ActionResult(success=True)

            cap._actions_by_name["browser.wait"].execute = opens  # type: ignore[method-assign]
            result = await cap.execute_action(Action(name="browser.wait", params={}))
            assert result.success, result.error
            observed = await _observe(cap, 2)
            state, meta = observed["browser.capture_state"], observed["browser.browser_meta"]
            assert isinstance(state, BrowserCaptureStateObs) and isinstance(meta, BrowserMetaObs)
            assert state.focus_moved == meta.focus_moved
            assert state.foreground_change == ForegroundChange(
                reason="opened", from_surface=opener, to_surface="page-2"
            )
            # Probed on the tab the capture shows.
            assert browser.probes[-1] == cap.default_session_id != browser.probes[0]
        finally:
            await cap.teardown()
