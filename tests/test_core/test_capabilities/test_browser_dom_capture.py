"""Capture across targets: how the DOM resolver reaches out-of-process frames.

The fake CDP below reproduces the behaviours of Chrome 152 the design rests
on, each observed against real headless Chrome while building it:

* ``Target.setAutoAttach`` on a session answers only *after* it has sent
  ``Target.attachedToTarget`` for every frame that already exists below it;
* the event arrives tagged with the parent's session id, and a nested frame
  is only reported once auto-attach is on for its own parent's session;
* a frame keeps its target id across a renderer swap but gets a new session;
* a renderer whose main thread is busy answers nothing until it is done —
  every frame of that renderer included.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest
from agent_cdp import EventScope

import spectragent.core.capabilities.browser.dom.serializers.text_serializer  # noqa: F401
from spectragent.core.capabilities.browser.dom import capture as capture_module
from spectragent.core.capabilities.browser.dom.capture import (
    FRAME_CAPTURE_TIMEOUT_S,
    MAX_FRAME_DEPTH,
    FrameTargets,
    capture_target,
)
from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
    REQUIRED_COMPUTED_STYLES,
    BrowserUseCleaner,
)
from spectragent.core.capabilities.browser.dom.models import FRAME_HANDLE_BASE, TargetCapture
from spectragent.core.capabilities.browser.watchdogs.dom_watchdog import DOMWatchdog
from tests._helpers.dom_payloads import Doc, El, compile_target


class ChromeLikeCDP:
    """Scripted CDP endpoint with per-session documents and frame children."""

    def __init__(self) -> None:
        self.is_connected = True
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self.callbacks: dict[str, list[Callable[..., Any]]] = {}
        #: parent session → [(target id, child session)]
        self.children: dict[str, list[tuple[str, str]]] = {}
        self.documents: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.frame_trees: dict[str, dict[str, Any]] = {}
        self.ax_nodes: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
        self.dead_sessions: set[str] = set()
        self.refuse_auto_attach: set[str] = set()
        #: Sessions whose renderer never answers (a spinning main thread).
        self.stuck_sessions: set[str] = set()
        #: Seconds a session's renderer takes to answer any command.
        self.delays: dict[str, float] = {}

    # CDPCommandProtocol --------------------------------------------------

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        self.callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        cbs = self.callbacks.get(method, [])
        if callback in cbs:
            cbs.remove(callback)

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> Any:
        params = dict(params or {})
        self.sent.append((method, params, session_id))
        if session_id in self.dead_sessions:
            raise RuntimeError("CDP error: Session with given id not found.")
        if session_id in self.stuck_sessions:
            await asyncio.Event().wait()
        if session_id in self.delays:
            await asyncio.sleep(self.delays[session_id])
        if method == "Target.setAutoAttach":
            if session_id in self.refuse_auto_attach:
                raise RuntimeError("CDP error: Not allowed")
            for target_id, child in self.children.get(session_id or "", []):
                self.attach(session_id or "", target_id, child)
            return {}
        if method == "DOM.getDocument":
            return self.documents.get(session_id or "", ({}, {}))[0]
        if method == "DOMSnapshot.captureSnapshot":
            return self.documents.get(session_id or "", ({}, {}))[1]
        if method == "Page.getFrameTree":
            return self.frame_trees.get(session_id or "")
        if method == "Accessibility.getFullAXTree":
            return {"nodes": self.ax_nodes.get((session_id, params.get("frameId")), [])}
        return {}

    # Scripted browser side -------------------------------------------------

    def fire(self, method: str, params: dict[str, Any], session_id: str | None) -> None:
        for callback in list(self.callbacks.get(method, [])):
            callback(params, session_id)

    def attach(self, parent: str, target_id: str, child: str, type_: str = "iframe") -> None:
        self.fire(
            "Target.attachedToTarget",
            {
                "sessionId": child,
                "targetInfo": {"targetId": target_id, "type": type_, "url": ""},
                "waitingForDebugger": False,
            },
            parent,
        )

    def detach(self, parent: str, target_id: str, child: str) -> None:
        self.fire("Target.detachedFromTarget", {"sessionId": child, "targetId": target_id}, parent)

    def sent_to(self, method: str) -> list[str | None]:
        return [sid for m, _, sid in self.sent if m == method]


def one_button_doc(frame_id: str, backend_id: int = 10) -> Doc:
    return Doc(
        frame_id,
        html_id=1,
        viewport=(300, 100),
        height=100,
        body=[El("button", backend_id, bounds=(2, 2, 40, 20), attrs={"id": f"btn-{frame_id}"})],
    )


def host_page(*iframe_frame_ids: str) -> Doc:
    return Doc(
        "MAIN",
        html_id=1,
        body=[
            El("iframe", 20 + i, bounds=(0, 100 * i, 300, 100), oopif_frame_id=fid)
            for i, fid in enumerate(iframe_frame_ids)
        ],
    )


def load(cdp: ChromeLikeCDP, session: str, doc: Doc) -> None:
    capture = compile_target(doc, session_id=session)
    cdp.documents[session] = (capture.raw_dom, capture.snapshot)


def tree() -> ChromeLikeCDP:
    """tab ─ CROSS (session cross) ─ NESTED (session nested)."""
    cdp = ChromeLikeCDP()
    cdp.children = {"tab": [("CROSS", "cross")], "cross": [("NESTED", "nested")]}
    load(cdp, "tab", host_page("CROSS"))
    load(
        cdp,
        "cross",
        Doc(
            "CROSS",
            html_id=1,
            viewport=(300, 100),
            height=100,
            body=[
                El("button", 10, bounds=(2, 2, 40, 20), attrs={"id": "btn-CROSS"}),
                El("iframe", 11, bounds=(0, 30, 200, 60), oopif_frame_id="NESTED"),
            ],
        ),
    )
    load(cdp, "nested", one_button_doc("NESTED"))
    return cdp


async def capture(
    cdp: ChromeLikeCDP,
    frames: FrameTargets | None,
    session: str | None = "tab",
    *,
    js_click_listeners: bool = False,
    frame_timeout_s: float = FRAME_CAPTURE_TIMEOUT_S,
) -> TargetCapture:
    return await capture_target(
        cdp,  # type: ignore[arg-type]
        session,
        frames=frames,
        js_click_listeners=js_click_listeners,
        required_computed_styles=REQUIRED_COMPUTED_STYLES,
        frame_timeout_s=frame_timeout_s,
    )


def watched(cdp: ChromeLikeCDP) -> FrameTargets:
    frames = FrameTargets(cdp)  # type: ignore[arg-type]
    frames.watch("tab")
    return frames


class TestDiscovery:
    async def test_existing_frames_are_captured_on_the_first_observation(self) -> None:
        cdp = tree()
        result = await capture(cdp, watched(cdp))

        cross = result.frames["CROSS"]
        nested = cross.frames["NESTED"]
        assert (cross.session_id, cross.target_id, cross.namespace) == ("cross", "CROSS", 1)
        assert (nested.session_id, nested.target_id, nested.namespace) == ("nested", "NESTED", 2)
        assert result.namespace == 0 and result.target_id is None

    async def test_each_target_is_captured_on_its_own_session(self) -> None:
        cdp = tree()
        await capture(cdp, watched(cdp))
        assert sorted(s or "" for s in cdp.sent_to("DOM.getDocument")) == ["cross", "nested", "tab"]
        assert sorted(s or "" for s in cdp.sent_to("DOMSnapshot.captureSnapshot")) == [
            "cross",
            "nested",
            "tab",
        ]

    async def test_auto_attach_is_turned_on_once_per_session(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)
        await capture(cdp, frames)
        assert sorted(s or "" for s in cdp.sent_to("Target.setAutoAttach")) == [
            "cross",
            "nested",
            "tab",
        ]
        params = next(p for m, p, _ in cdp.sent if m == "Target.setAutoAttach")
        assert params["flatten"] is True
        assert params["waitForDebuggerOnStart"] is False
        assert params["filter"] == [{"type": "iframe"}]

    async def test_a_frame_created_later_is_picked_up_by_the_next_observation(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)

        load(cdp, "late", one_button_doc("LATE"))
        cdp.attach("tab", "LATE", "late")  # auto-attach is already on
        result = await capture(cdp, frames)
        assert result.frames["LATE"].namespace == 3

    async def test_the_browser_session_is_never_auto_attached(self) -> None:
        cdp = tree()
        await capture(cdp, None, session=None)
        assert cdp.sent_to("Target.setAutoAttach") == []

    async def test_a_dom_watchdog_without_a_tab_session_does_not_discover_frames(self) -> None:
        cdp = tree()
        wd = DOMWatchdog(cdp, enable_cross_site_iframe_capture=True)  # type: ignore[arg-type]
        await wd._capture_cdp_data()
        assert cdp.sent_to("Target.setAutoAttach") == []

    async def test_depth_is_bounded(self) -> None:
        cdp = ChromeLikeCDP()
        chain = [f"f{i}" for i in range(MAX_FRAME_DEPTH + 3)]
        cdp.children["tab"] = [(chain[0].upper(), chain[0])]
        load(cdp, "tab", host_page(chain[0].upper()))
        for parent, child in zip(chain, chain[1:], strict=False):
            cdp.children[parent] = [(child.upper(), child)]
            load(cdp, parent, host_page(child.upper()))

        result = await capture(cdp, watched(cdp))
        depth = 0
        while result.frames:
            result = next(iter(result.frames.values()))
            depth += 1
        assert depth == MAX_FRAME_DEPTH


class TestFrameTable:
    async def test_other_tabs_and_other_target_types_are_ignored(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        cdp.attach("some-other-tab", "THEIRS", "theirs")
        cdp.attach("tab", "WORKER", "worker", type_="worker")
        cdp.attach(None, "PAGE", "page", type_="iframe")  # type: ignore[arg-type]
        result = await capture(cdp, frames)
        assert set(result.frames) == {"CROSS"}

    async def test_a_detached_frame_leaves_with_its_subtree(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)

        cdp.children = {}
        cdp.detach("tab", "CROSS", "cross")  # parent first: nested must not linger
        assert await frames.children("tab") == []
        assert await frames.children("cross") == []
        assert await frames.children("nested") == []

    async def test_a_frame_that_swaps_process_keeps_its_namespace(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)

        cdp.detach("tab", "CROSS", "cross")
        cdp.attach("tab", "CROSS", "cross-2")  # same frame, new renderer
        (frame,) = await frames.children("tab")
        assert (frame.session_id, frame.namespace) == ("cross-2", 1)

    async def test_namespaces_are_never_reused(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)
        cdp.detach("tab", "CROSS", "cross")
        cdp.attach("tab", "OTHER", "other")
        (frame,) = await frames.children("tab")
        assert frame.namespace == 3  # CROSS had 1, NESTED 2

    async def test_a_refused_auto_attach_is_retried_next_observation(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        cdp.refuse_auto_attach = {"tab"}
        assert (await capture(cdp, frames)).frames == {}

        cdp.refuse_auto_attach = set()
        assert set((await capture(cdp, frames)).frames) == {"CROSS"}

    async def test_close_releases_the_callbacks(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        assert cdp.callbacks["Target.attachedToTarget"]
        frames.close()
        frames.close()
        assert not any(cdp.callbacks.values())

    async def test_dom_watchdog_detach_releases_the_callbacks(self) -> None:
        cdp = tree()
        wd = DOMWatchdog(cdp, enable_cross_site_iframe_capture=True)  # type: ignore[arg-type]
        scope = _Scope()
        wd.attach(scope, session_id="tab")  # type: ignore[arg-type]
        assert cdp.callbacks["Target.attachedToTarget"]
        wd.detach(scope, "tab")
        assert not any(cdp.callbacks.values())


class _Scope(EventScope):
    def __init__(self) -> None:
        super().__init__("test-tab")


class TestTheProfileDecides:
    async def test_off_no_frame_is_discovered_or_listened_for(self) -> None:
        cdp = tree()
        wd = DOMWatchdog(cdp, enable_cross_site_iframe_capture=False)  # type: ignore[arg-type]
        scope = _Scope()
        wd.attach(scope, session_id="tab")  # type: ignore[arg-type]
        wd._session_id = "tab"
        captured, _dpr = await wd._capture_cdp_data()

        assert not any(cdp.callbacks.values())
        assert cdp.sent_to("Target.setAutoAttach") == []
        assert captured.frames == {}
        assert sorted(s or "" for s in cdp.sent_to("DOM.getDocument")) == ["tab"]
        wd.detach(scope, "tab")

    async def test_on_every_frame_target_is_captured(self) -> None:
        cdp = tree()
        wd = DOMWatchdog(cdp, enable_cross_site_iframe_capture=True)  # type: ignore[arg-type]
        scope = _Scope()
        wd.attach(scope, session_id="tab")  # type: ignore[arg-type]
        captured, _dpr = await wd._capture_cdp_data()
        assert set(captured.frames) == {"CROSS"}
        assert set(captured.frames["CROSS"].frames) == {"NESTED"}
        wd.detach(scope, "tab")


class TestAFrameCannotStallTheObservation:
    """One deadline for every frame target; the tab's own capture is never cut."""

    async def test_a_frame_that_never_answers_is_left_out(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)  # frames discovered while healthy
        cdp.stuck_sessions = {"cross"}

        started = time.monotonic()
        result = await capture(cdp, frames, frame_timeout_s=0.1)
        assert time.monotonic() - started < 1.0
        assert result.frames == {}
        assert result.raw_dom  # the tab itself was captured in full

    async def test_its_iframe_keeps_its_own_index_and_nothing_of_its_content(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)
        cdp.stuck_sessions = {"cross"}
        state = await BrowserUseCleaner().clean(
            await capture(cdp, frames, frame_timeout_s=0.1), (1280, 720)
        )
        assert set(state.selector_map) == {20}
        assert state.selector_map[20].content_document is None

    async def test_a_stuck_nested_frame_costs_only_itself(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)
        cdp.stuck_sessions = {"nested"}

        result = await capture(cdp, frames, frame_timeout_s=0.1)
        assert set(result.frames) == {"CROSS"}
        assert result.frames["CROSS"].frames == {}

    async def test_the_deadline_is_shared_down_a_chain_of_slow_frames(self) -> None:
        """Each level is discovered only after its parent answers.

        Five slow levels at 0.1 s each would run 0.5 s under a per-frame
        limit; one deadline for the observation stops them all at 0.3 s.
        """
        cdp = ChromeLikeCDP()
        chain = [f"f{i}" for i in range(MAX_FRAME_DEPTH)]
        cdp.children["tab"] = [(chain[0].upper(), chain[0])]
        load(cdp, "tab", host_page(chain[0].upper()))
        for parent, child in zip(chain, chain[1:], strict=False):
            cdp.children[parent] = [(child.upper(), child)]
            load(cdp, parent, host_page(child.upper()))
        load(cdp, chain[-1], one_button_doc(chain[-1].upper()))
        cdp.delays = dict.fromkeys(chain, 0.1)

        started = time.monotonic()
        await capture(cdp, watched(cdp), frame_timeout_s=0.3)
        assert time.monotonic() - started < 0.45

    async def test_frame_discovery_that_never_answers_is_bounded_too(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        cdp.stuck_sessions = {"cross"}  # setAutoAttach on cross hangs

        started = time.monotonic()
        result = await capture(cdp, frames, frame_timeout_s=0.1)
        assert time.monotonic() - started < 1.0
        assert result.frames == {}

    async def test_a_frame_that_answers_again_is_captured_again(self) -> None:
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)
        cdp.stuck_sessions = {"cross"}
        assert (await capture(cdp, frames, frame_timeout_s=0.05)).frames == {}

        cdp.stuck_sessions = set()
        assert set((await capture(cdp, frames)).frames) == {"CROSS"}


class TestNothingRunsInAFramesPageWorld:
    async def test_the_click_listener_scan_runs_in_the_tab_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanned: list[str | None] = []

        async def fake_scan(_send: Any, session_id: str | None = None) -> set[int]:
            scanned.append(session_id)
            return {10}

        monkeypatch.setattr(capture_module.event_listeners, "detect_js_click_listeners", fake_scan)
        cdp = tree()
        result = await capture(cdp, watched(cdp), js_click_listeners=True)

        assert scanned == ["tab"]
        assert result.js_click_listener_ids == {10}
        assert result.frames["CROSS"].js_click_listener_ids is None
        assert result.frames["CROSS"].frames["NESTED"].js_click_listener_ids is None
        assert not any(
            method.startswith("Runtime.") and session in {"cross", "nested"}
            for method, _, session in cdp.sent
        )


class TestCaptureContents:
    async def test_accessibility_nodes_of_every_same_target_frame_are_merged(self) -> None:
        cdp = tree()
        cdp.frame_trees["tab"] = {
            "frameTree": {
                "frame": {"id": "MAIN"},
                "childFrames": [{"frame": {"id": "SAME"}}],
            }
        }
        cdp.ax_nodes[("tab", "MAIN")] = [{"backendDOMNodeId": 1, "role": {"value": "RootWebArea"}}]
        cdp.ax_nodes[("tab", "SAME")] = [{"backendDOMNodeId": 52, "role": {"value": "button"}}]

        result = await capture(cdp, watched(cdp))
        assert [n["backendDOMNodeId"] for n in result.ax_tree["nodes"]] == [1, 52]
        assert [
            p.get("frameId")
            for m, p, s in cdp.sent
            if m == "Accessibility.getFullAXTree" and s == "tab"
        ] == [
            "MAIN",
            "SAME",
        ]

    async def test_without_a_frame_tree_the_main_frame_is_still_read(self) -> None:
        cdp = tree()
        cdp.ax_nodes[("tab", None)] = [{"backendDOMNodeId": 1, "role": {"value": "RootWebArea"}}]
        result = await capture(cdp, watched(cdp))
        assert [n["backendDOMNodeId"] for n in result.ax_tree["nodes"]] == [1]

    async def test_a_frame_whose_session_died_stays_opaque(self) -> None:
        """Detached between discovery and capture: an empty capture, not a crash."""
        cdp = tree()
        frames = watched(cdp)
        await capture(cdp, frames)
        cdp.dead_sessions = {"cross"}

        result = await capture(cdp, frames)
        state = await BrowserUseCleaner().clean(result, (1280, 720))
        keys = set(state.selector_map)
        assert keys == {20}  # the frame owner itself: its document is not in the tree
        assert not any(k >= FRAME_HANDLE_BASE for k in keys)


@pytest.fixture(autouse=True)
def _quiet_capture_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("ERROR", logger="spectragent.core.capabilities.browser.dom.capture")
