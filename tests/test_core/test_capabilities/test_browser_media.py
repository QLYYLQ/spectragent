"""Media playback state: reading, matching, comparing, judging and the alignment request (``media.py``).

``docs/architecture_design.md`` §4.4 "截图的媒体状态", §4.14 "恢复：重定位与校验",
§5.3. The pure half and the fake-CDP half; a real Chrome runs the in-page
probes in ``test_browser_media_live.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents._browser_state import render_browser_state, render_media_state
from spectragent.core.capabilities.base import RestorationAlignment, RestorationSupport
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.media import (
    MEDIA_WORLD,
    MediaElementState,
    MediaRead,
    MediaReading,
    MediaRect,
    MediaState,
    PendingAlignment,
    align_media,
    compare_media,
    match_media,
    media_state,
    read_media,
)
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.capabilities.browser.restoration import measure, media_measure
from spectragent.core.loops.restore.checks import judge, media_failed
from spectragent.core.trace.encoding import decode_fragment, encode_fragment
from spectragent.schemas.restoration import (
    REASON_ORDER,
    MediaAlignment,
    MediaComparison,
    MediaEvidence,
    RestorationEvidence,
    RestorationReason,
)

# ── builders ──────────────────────────────────────────────────────────


def element(
    *,
    frame: str = "main",
    order: int = 0,
    tag: str = "video",
    x: float = 240,
    y: float = 24,
    source: str = "blob",
    url: str | None = None,
    duration: float | None = 210.769,
    live: bool = False,
    time: float | None = 19.0,
    paused: bool | None = True,
    after: float | None = ...,  # type: ignore[assignment]
) -> MediaElementState:
    reading = MediaReading(current_time=time, paused=paused, seeking=False, ready_state=4)
    later = reading if after is ... else MediaReading(current_time=after, paused=paused)
    return MediaElementState(
        frame=frame,
        order=order,
        tag=tag,  # type: ignore[arg-type]
        rect=MediaRect(x=x, y=y, width=400, height=672),
        source_kind=source,  # type: ignore[arg-type]
        url=url,
        duration=duration,
        live=live,
        before=reading,
        after=later,
    )


def state(*elements: MediaElementState, **fields: Any) -> MediaState:
    return MediaState(
        read_before=fields.pop("read_before", "measured"),
        read_after=fields.pop("read_after", "measured"),
        elements=list(elements),
        **fields,
    )


def observation(media: MediaState | None) -> SimpleNamespace:
    return SimpleNamespace(screenshot=ScreenshotObs(image_bytes=b"", media=media))


def raw(**fields: Any) -> dict[str, Any]:
    """One element as the in-page read returns it."""
    base: dict[str, Any] = {
        "frame": "main",
        "order": 0,
        "tag": "video",
        "x": 240.0,
        "y": 24.0,
        "width": 400.0,
        "height": 672.0,
        "source_kind": "blob",
        "url": None,
        "duration": 210.769,
        "live": False,
        "reading": {
            "current_time": 41.0,
            "paused": True,
            "seeking": False,
            "ready_state": 4,
            "ended": False,
        },
    }
    base.update(fields)
    return base


# ── combining the two reads ───────────────────────────────────────────


def test_the_two_reads_join_by_frame_and_order() -> None:
    before = MediaRead(
        elements=(raw(), raw(order=1, reading={"current_time": 3.0, "paused": False}))
    )
    after = MediaRead(
        elements=(raw(reading={"current_time": 41.0, "paused": True}),),
        unreachable=({"frame": "main/1", "x": 1288, "y": 0, "width": 300, "height": 150},),
    )
    media = media_state(before, after)
    assert (media.read_before, media.read_after, media.detail) == ("measured", "measured", None)
    first, second = media.elements
    assert first.label == "main#0 video"
    assert first.before is not None and first.after is not None
    assert first.before.current_time == first.after.current_time == 41.0
    assert first.rect == MediaRect(x=240, y=24, width=400, height=672)
    # Seen by one read only: the other reading is absent, not invented.
    assert second.before is not None and second.after is None
    assert [frame.frame for frame in media.unreachable_frames] == ["main/1"]


def test_a_read_that_did_not_answer_is_unknown_not_empty() -> None:
    media = media_state(MediaRead(error="the media read got no answer within 1s"), MediaRead())
    assert (media.read_before, media.read_after) == ("unknown", "measured")
    assert media.known
    assert media.detail == "before: the media read got no answer within 1s"
    neither = media_state(MediaRead(error="x"), MediaRead(error="y"))
    assert not neither.known


def test_malformed_elements_are_dropped_not_guessed() -> None:
    media = media_state(MediaRead(elements=(raw(order="0"), raw(tag="img"), raw())), MediaRead())
    assert [e.label for e in media.elements] == ["main#0 video"]


# ── reading through CDP ───────────────────────────────────────────────


def frame_value(path: str = "main", *elements: dict[str, Any], children: Any = ()) -> dict:
    """One frame's own read, as the in-page probe returns it."""
    return {
        "path": path,
        "elements": [{k: v for k, v in e.items() if k != "frame"} for e in elements],
        "children": list(children),
        "truncated": False,
    }


class _ScriptedCDP:
    """Answers the media world, the frame tree and each frame's read; can stall or fail.

    *frames* maps a frame id to its read (a value, or an exception to raise);
    *tree* is the tab session's frame ids.
    """

    def __init__(
        self,
        value: Any = None,
        *,
        stall: bool = False,
        world: Any = None,
        frames: dict[str, Any] | None = None,
        tree: list[str] | None = None,
    ) -> None:
        self.frames = frames or {"F": value if value is not None else frame_value("main", raw())}
        self.tree = tree
        self.stall = stall
        self.world = world if world is not None else {"executionContextId": 42}
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self._contexts: dict[int, str] = {}

    async def send(self, method: str, params: dict[str, Any], session_id: str | None = None) -> Any:
        self.sent.append((method, params, session_id))
        if self.stall:
            await asyncio.sleep(10)
        if method == "Page.getFrameTree":
            ids = self.tree or ["F"]
            return {
                "frameTree": {
                    "frame": {"id": ids[0]},
                    "childFrames": [{"frame": {"id": i}} for i in ids[1:]],
                }
            }
        if method == "Page.createIsolatedWorld":
            if self.world.get("executionContextId") is None:
                return self.world
            context = 100 + len(self._contexts)
            self._contexts[context] = params["frameId"]
            return {"executionContextId": context}
        frame = self._contexts.get(int(params.get("contextId") or 0), "")
        answer = self.frames.get(frame, ["no"])
        if isinstance(answer, BaseException):
            raise answer
        if answer == "hang":
            await asyncio.sleep(10)
        return {"result": {"type": "object", "value": answer}}


async def test_each_frame_is_read_in_its_own_isolated_world() -> None:
    cdp = _ScriptedCDP()
    read = await read_media(cdp, session_id="S", frame_id="F", frames=None, timeout_s=1.0)  # type: ignore[arg-type]
    assert read.ok and len(read.elements) == 1
    assert read.addresses == {"main": ("S", "F")}
    worlds = [p for m, p, _s in cdp.sent if m == "Page.createIsolatedWorld"]
    assert worlds == [{"frameId": "F", "worldName": MEDIA_WORLD}]
    (evaluate,) = [p for m, p, _s in cdp.sent if m == "Runtime.evaluate"]
    assert evaluate["returnByValue"] is True


@pytest.mark.parametrize(
    ("cdp", "frame", "why"),
    [
        (_ScriptedCDP(), None, "the tab's main frame is not known"),
        (_ScriptedCDP(stall=True), "F", "the media read got no answer within 0.05s"),
        (
            _ScriptedCDP(world={}),
            "F",
            "the media read failed: the media world has no execution context",
        ),
        (
            _ScriptedCDP(value=["no"]),
            "F",
            "the media read failed: the media read returned no result",
        ),
    ],
)
async def test_a_read_that_cannot_be_made_says_why(cdp: Any, frame: str | None, why: str) -> None:
    read = await read_media(cdp, session_id="S", frame_id=frame, frames=None, timeout_s=0.05)
    assert read.error == why and not read.elements


async def test_child_frames_are_placed_in_the_top_viewport() -> None:
    cdp = _ScriptedCDP(
        frames={
            "F": frame_value(
                "main",
                raw(x=10.0, y=20.0),
                children=[{"frame": "main/0", "x": 100, "y": 200, "width": 300, "height": 150}],
            ),
            "G": frame_value("main/0", raw(x=5.0, y=6.0)),
        },
        tree=["F", "G"],
    )
    read = await read_media(cdp, session_id="S", frame_id="F", frames=None, timeout_s=1.0)  # type: ignore[arg-type]
    placed = {(e["frame"], e["order"]): (e["x"], e["y"]) for e in read.elements}
    assert placed == {("main", 0): (10.0, 20.0), ("main/0", 0): (105.0, 206.0)}
    assert read.unreachable == ()
    assert read.addresses == {"main": ("S", "F"), "main/0": ("S", "G")}


@pytest.mark.parametrize("failure", [RuntimeError("frame gone"), "hang"], ids=["fails", "hangs"])
async def test_a_child_frame_whose_read_does_not_answer_is_unreachable(failure: Any) -> None:
    cdp = _ScriptedCDP(
        frames={
            "F": frame_value(
                "main",
                raw(),
                children=[{"frame": "main/0", "x": 100, "y": 200, "width": 300, "height": 150}],
            ),
            "G": failure,
        },
        tree=["F", "G"],
    )
    read = await read_media(cdp, session_id="S", frame_id="F", frames=None, timeout_s=0.2)  # type: ignore[arg-type]
    assert read.ok, "the main frame answered: the read stands"
    assert [e["frame"] for e in read.elements] == ["main"]
    assert read.unreachable == (
        {"frame": "main/0", "x": 100.0, "y": 200.0, "width": 300.0, "height": 150.0},
    )
    state = media_state(read, read)
    assert [f.frame for f in state.unreachable_frames] == ["main/0"]


async def test_a_cross_site_frame_is_read_on_its_own_session() -> None:
    from spectragent.core.capabilities.browser.dom.capture import FrameTarget

    class _Frames:
        watched: list[str] = []

        def watch(self, root_session_id: str) -> None:
            self.watched.append(root_session_id)

        async def children(self, session_id: str) -> list[FrameTarget]:
            if session_id == "S":
                return [FrameTarget(target_id="OOPIF", session_id="C", namespace=1)]
            return []

    cdp = _ScriptedCDP(
        frames={
            "F": frame_value(
                "main",
                children=[{"frame": "main/1", "x": 340, "y": 200, "width": 300, "height": 100}],
            ),
            "OOPIF": frame_value("main/1", raw(x=0.0, y=0.0)),
        },
        tree=["F"],
    )
    read = await read_media(
        cdp,  # type: ignore[arg-type]
        session_id="S",
        frame_id="F",
        frames=_Frames(),  # type: ignore[arg-type]
        timeout_s=1.0,
    )
    (only,) = read.elements
    assert (only["frame"], only["x"], only["y"]) == ("main/1", 340.0, 200.0)
    assert read.addresses["main/1"] == ("C", "OOPIF")
    sessions = {s for m, p, s in cdp.sent if m == "Runtime.evaluate"}
    assert sessions == {"S", "C"}
    assert _Frames.watched == ["S"], "the tab's first read starts its frame discovery"
    assert read.unreachable == ()


# ── matching across runs ──────────────────────────────────────────────


def test_same_frame_order_and_place_match() -> None:
    (match,) = match_media([element()], [element(time=10.0)], tolerance_px=2.0)
    assert match.correspondence == "matched"


def test_blob_urls_are_never_compared_ordinary_urls_are() -> None:
    blob = match_media([element()], [element()], tolerance_px=2.0)
    assert blob[0].correspondence == "matched"
    one = element(source="url", url="https://cdn.test/a.mp4")
    other = element(source="url", url="https://cdn.test/b.mp4")
    (missing, extra) = match_media([one], [other], tolerance_px=2.0)
    assert (missing.correspondence, extra.correspondence) == ("missing", "extra")


def test_a_different_duration_or_place_is_not_the_same_element() -> None:
    assert match_media([element()], [element(duration=99.0)], tolerance_px=2.0)[
        0
    ].correspondence == ("missing")
    assert match_media([element()], [element(duration=None)], tolerance_px=2.0)[
        0
    ].correspondence == ("matched")
    assert match_media([element(x=240)], [element(x=243)], tolerance_px=2.0)[0].correspondence == (
        "missing"
    )
    assert match_media([element(x=240)], [element(x=242)], tolerance_px=2.0)[0].correspondence == (
        "matched"
    )
    assert match_media([element()], [element(frame="main/0")], tolerance_px=2.0)[
        0
    ].correspondence == ("missing")


def test_order_and_place_that_disagree_are_ambiguous() -> None:
    # A media element inserted before it: the one in the recorded place has another order.
    shifted = match_media([element(order=0)], [element(order=1)], tolerance_px=2.0)
    assert [m.correspondence for m in shifted] == ["ambiguous", "extra"]
    # Two elements stacked in one place: position cannot single one out.
    stacked = match_media([element()], [element(), element(order=1)], tolerance_px=2.0)
    assert stacked[0].correspondence == "ambiguous"
    assert "2 candidate(s)" in stacked[0].detail


# ── comparing ─────────────────────────────────────────────────────────


def test_a_comparison_needs_media_state_on_both_sides() -> None:
    assert compare_media(None, state(element()), tolerance_px=2.0) == (
        None,
        "the recorded screenshot carries no media state",
    )
    unknown = state(read_before="unknown", read_after="unknown", detail="before: stalled")
    comparisons, why = compare_media(state(), unknown, tolerance_px=2.0)
    assert comparisons is None and why == "the restored media state is unknown (before: stalled)"
    assert compare_media(state(), state(), tolerance_px=2.0) == ([], "")


def test_the_comparison_uses_the_reading_both_sides_took() -> None:
    recorded = state(element(time=41.0, paused=True))
    restored = state(element(time=10.0, paused=True))
    (item,) = compare_media(recorded, restored, tolerance_px=2.0)[0] or []
    assert (item.reading, item.recorded_time, item.restored_time) == ("before", 41.0, 10.0)
    no_before = MediaElementState.model_validate(
        {**element(time=10.0).model_dump(), "before": None}
    )
    (later,) = compare_media(recorded, state(no_before), tolerance_px=2.0)[0] or []
    assert later.reading == "after"


def _evidence(media: MediaEvidence) -> RestorationEvidence:
    return RestorationEvidence(capability_id="browser", media=media)


def _reasons(media: MediaEvidence, tolerance: float = 1.0) -> list[RestorationReason]:
    return judge(
        0,
        [_evidence(media)],
        [],
        verify="warn",
        screenshot_max_distance=5,
        media_time_tolerance_s=tolerance,
    ).reasons


def _compared(recorded: MediaState, restored: MediaState) -> MediaEvidence:
    comparisons, detail = compare_media(recorded, restored, tolerance_px=2.0)
    return MediaEvidence(comparisons=comparisons, detail=detail)


def test_media_mismatch_comes_before_the_screenshot_distance() -> None:
    assert REASON_ORDER.index("media_mismatch") == REASON_ORDER.index("screenshot_distance") - 1
    assert REASON_ORDER.index("media_mismatch") > REASON_ORDER.index("signature_mismatch")
    evidence = RestorationEvidence(
        capability_id="browser",
        screenshot_distance=9,
        media=_compared(state(element(time=41.0)), state(element(time=10.0))),
    )
    check = judge(
        0, [evidence], [], verify="warn", screenshot_max_distance=5, media_time_tolerance_s=1.0
    )
    assert check.reasons == ["media_mismatch", "screenshot_distance"]
    assert check.reason == "media_mismatch"


def test_the_position_is_judged_against_the_tolerance() -> None:
    near = _compared(state(element(time=19.0)), state(element(time=19.9)))
    assert _reasons(near) == []
    far = _compared(state(element(time=19.0)), state(element(time=41.0)))
    assert _reasons(far) == ["media_mismatch"]
    assert _reasons(far, tolerance=30.0) == []


def test_a_paused_state_difference_fails_whatever_the_position() -> None:
    playing = _compared(state(element(paused=True)), state(element(paused=False)))
    assert _reasons(playing, tolerance=60.0) == ["media_mismatch"]


@pytest.mark.parametrize(
    ("recorded", "restored"),
    [
        (state(element()), state()),  # missing
        (state(), state(element())),  # extra
        (state(element()), state(element(), element(order=1))),  # ambiguous
        (state(element(live=True, duration=None)), state(element(live=True, duration=None))),
    ],
    ids=["missing", "extra", "ambiguous", "live"],
)
def test_presence_ambiguity_and_live_streams_fail(
    recorded: MediaState, restored: MediaState
) -> None:
    assert _reasons(_compared(recorded, restored)) == ["media_mismatch"]


def test_unknown_values_never_decide() -> None:
    assert _reasons(MediaEvidence(comparisons=None, detail="no media state")) == []
    unknown_time = _compared(state(element(time=None)), state(element(time=50.0)))
    assert _reasons(unknown_time) == []
    unknown_paused = _compared(state(element(paused=None)), state(element(paused=False)))
    assert _reasons(unknown_paused) == []
    assert _reasons(MediaEvidence(alignment=[MediaAlignment(element="x", outcome="unknown")])) == []


@pytest.mark.parametrize(
    ("outcome", "fails"),
    [
        ("aligned", False),
        ("already_aligned", False),
        ("unknown", False),
        ("failed", True),
        ("not_restorable", True),
    ],
)
def test_alignment_outcomes(outcome: str, fails: bool) -> None:
    media = MediaEvidence(
        comparisons=[],
        alignment=[MediaAlignment(element="main#0 video", outcome=outcome)],  # type: ignore[arg-type]
    )
    assert media_failed(media, time_tolerance_s=1.0) is fails


# ── the screenshot fragment carries it ────────────────────────────────


def test_media_state_reads_back_from_the_trace() -> None:
    shot = ScreenshotObs(
        image_bytes=b"\x89PNG\r\n\x1a\nxx",
        viewport_width=1280,
        viewport_height=720,
        media=state(element(), unreachable_frames=[]),
    )
    encoded = encode_fragment(0, shot)
    assert encoded is not None and encoded.artifact is not None
    payload = json.loads(encoded.file.data)
    assert payload["media"]["elements"][0]["before"]["current_time"] == 19.0
    restored = decode_fragment(encoded.key, ScreenshotObs, payload, encoded.artifact.data)
    assert restored.media == shot.media


def test_no_media_state_writes_what_a_screenshot_always_wrote() -> None:
    encoded = encode_fragment(0, ScreenshotObs(image_bytes=b"\x89PNG\r\n\x1a\nxx"))
    assert encoded is not None
    assert "media" not in json.loads(encoded.file.data)


def test_measure_reports_media_only_with_a_screenshot_or_an_alignment() -> None:
    bare = SimpleNamespace()
    assert media_measure(bare, bare, tolerance_px=2.0, alignment=None) is None
    aligned = media_measure(bare, bare, tolerance_px=2.0, alignment=[], alignment_detail="none")
    assert aligned is not None and aligned.alignment == [] and aligned.comparisons is None
    old = observation(None)  # recorded before media state existed
    evidence = measure(
        old,
        observation(state(element())),
        capability_id="browser",
        document_misses=None,
        media=media_measure(old, observation(state(element())), tolerance_px=2.0, alignment=None),
    )
    assert evidence.media is not None and evidence.media.comparisons is None
    assert evidence.media.detail == "the recorded screenshot carries no media state"


# ── the alignment request ─────────────────────────────────────────────


@pytest.mark.parametrize(("value", "valid"), [(0.25, True), (0, False), (11, False)])
def test_the_media_read_budget_is_a_capability_knob(value: float, valid: bool) -> None:
    import yaml
    from pydantic import ValidationError

    from spectragent.core.composer import ConfigComposer
    from spectragent.core.registries import collect_registrars

    path = Path(__file__).resolve().parents[3] / "configs/ui_tars_local.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["environment"][0]["capabilities"][0]["media_state_timeout_s"] = value
    composer = ConfigComposer(registrars=collect_registrars())
    if not valid:
        with pytest.raises(ValidationError):
            composer.compose(raw)
        return
    cap = next(iter(composer.compose(raw).single("environment").capabilities.values()))
    assert cap.media_state_timeout_s == value


@pytest.mark.parametrize("agent_id", ["skyvern", "browser_use"])
def test_show_media_state_is_an_agent_knob(agent_id: str) -> None:
    import yaml

    from spectragent.core.composer import ConfigComposer
    from spectragent.core.registries import collect_registrars

    path = Path(__file__).resolve().parents[3] / "configs/ui_tars_local.yaml"
    raw = yaml.safe_load(path.read_text())
    block = raw["agent"][0]
    raw["agent"][0] = {
        "agent_id": agent_id,
        "id": block["id"],
        "show_media_state": True,
        "agent_loop": block["agent_loop"],
        "llm": block["llm"],
        "memory": block["memory"],
    }
    agent = ConfigComposer(registrars=collect_registrars()).compose(raw).single("agent")
    assert agent.show_media_state is True


def test_the_capability_offers_alignment_beside_restoration() -> None:
    cap = BrowserCapability()
    assert isinstance(cap, RestorationSupport) and isinstance(cap, RestorationAlignment)
    assert cap.media_state_timeout_s == 1.0


def test_a_request_belongs_to_the_next_cycle_only() -> None:
    pending = PendingAlignment(recorded=None, seek_timeout_s=2.0)
    assert pending.bind(4) and pending.due(4)
    assert not pending.bind(5) and not pending.due(5)
    assert pending.take().detail == "no capture ran after the alignment was requested"


async def test_the_capability_keeps_a_request_for_one_observation() -> None:
    cap = BrowserCapability()
    recorded = observation(state(element()))
    await cap.align_next_observation(recorded, media_seek_timeout_s=1.5)
    await cap.begin_observation(1)
    pending = cap._media_alignment
    assert pending is not None and pending.cycle == 1 and pending.seek_timeout_s == 1.5
    assert pending.recorded == state(element())
    # Never taken by a check: a later observation drops it.
    await cap.begin_observation(2)
    assert cap._media_alignment is None


async def test_the_check_takes_the_request_whether_or_not_a_capture_used_it() -> None:
    cap = BrowserCapability()
    recorded = observation(state(element()))
    await cap.align_next_observation(recorded, media_seek_timeout_s=2.0)
    evidence = await cap.verify_restoration(recorded, observation(state(element())))
    assert evidence.media is not None and evidence.media.alignment == []
    assert evidence.media.alignment_detail == "no capture ran after the alignment was requested"
    assert cap._media_alignment is None
    again = await cap.verify_restoration(recorded, observation(state(element())))
    assert again.media is not None and again.media.alignment is None


class _AlignCDP:
    """A page whose one video reads as *live* and answers alignments as scripted."""

    def __init__(self, live: dict[str, Any], answers: list[dict[str, Any]]) -> None:
        self.live = live
        self.answers = list(answers)
        self.calls: list[str] = []

    async def send(self, method: str, params: dict[str, Any], session_id: str | None = None) -> Any:
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F"}}}
        expression = params["expression"]
        if "framePath" in expression:
            self.calls.append(expression.rsplit("})(", 1)[1].strip())
            return {"result": {"value": self.answers.pop(0)}}
        return {"result": {"value": frame_value("main", self.live)}}


def _answer(time: float, paused: bool, *, seeked: bool = True, played: bool | None = None) -> dict:
    reading = {"current_time": time, "paused": paused, "seeking": not seeked, "ready_state": 4}
    return {
        "found": True,
        "before": {"current_time": 10.0, "paused": False},
        "seeked": seeked,
        "seek_ms": 40.0 if seeked else None,
        "played": played,
        "reached": reading,
    }


async def _align(cdp: _AlignCDP, recorded: MediaState) -> list[MediaAlignment]:
    report = await align_media(
        cdp,  # type: ignore[arg-type]
        session_id="S",
        frame_id="F",
        frames=None,
        recorded=recorded,
        tolerance_px=2.0,
        read_timeout_s=1.0,
        seek_timeout_s=2.0,
    )
    return report.records


async def test_a_paused_source_is_paused_and_seeked() -> None:
    cdp = _AlignCDP(raw(reading={"current_time": 10.0, "paused": False}), [_answer(41.0, True)])
    (record,) = await _align(cdp, state(element(time=41.0, paused=True)))
    assert (record.outcome, record.action, record.target_time) == (
        "aligned",
        "pause_and_seek",
        41.0,
    )
    assert (record.from_time, record.from_paused, record.reached_time) == (10.0, False, 41.0)
    assert cdp.calls == ['"main", 0, 41.0, false, 2000)']


async def test_a_playing_source_is_seeked_then_played() -> None:
    cdp = _AlignCDP(raw(), [_answer(72.0, False, played=True)])
    (record,) = await _align(cdp, state(element(time=72.0, paused=False)))
    assert (record.outcome, record.action) == ("aligned", "seek_and_play")
    assert "controls" in record.detail
    assert cdp.calls == ['"main", 0, 72.0, true, 2000)']


async def test_a_failed_seek_is_undone_to_the_position_and_play_state_before() -> None:
    cdp = _AlignCDP(
        raw(reading={"current_time": 10.0, "paused": False}),
        [_answer(150.0, True, seeked=False), _answer(10.0, False, played=True)],
    )
    (record,) = await _align(cdp, state(element(time=150.0, paused=True)))
    assert record.outcome == "failed" and record.rolled_back is True
    assert record.seek_ms is None and record.seeking is True
    assert record.detail.startswith("the seek did not complete within 2s")
    # The undo targets where the element stood before: 10 s, playing.
    assert cdp.calls[1] == '"main", 0, 10.0, true, 2000)'


async def test_what_cannot_be_aligned_is_recorded_never_attempted() -> None:
    already = _AlignCDP(raw(), [])
    (record,) = await _align(already, state(element(time=41.0, paused=True)))
    assert record.outcome == "already_aligned" and already.calls == []
    gone = _AlignCDP(raw(x=500.0), [])
    (missing,) = await _align(gone, state(element(time=41.0)))
    assert missing.outcome == "not_restorable" and missing.detail.startswith("missing")
    live = _AlignCDP(raw(live=True, duration=None), [])
    (stream,) = await _align(live, state(element(live=True, duration=None)))
    assert stream.outcome == "not_restorable" and "live" in stream.detail
    unknown = _AlignCDP(raw(), [])
    (no_target,) = await _align(unknown, state(element(time=None)))
    assert no_target.outcome == "unknown" and unknown.calls == []


async def test_old_evidence_aligns_nothing_and_says_so() -> None:
    report = await align_media(
        _AlignCDP(raw(), []),  # type: ignore[arg-type]
        session_id="S",
        frame_id="F",
        frames=None,
        recorded=None,
        tolerance_px=2.0,
        read_timeout_s=1.0,
        seek_timeout_s=2.0,
    )
    assert (
        report.records == [] and report.detail == "the recorded screenshot carries no media state"
    )


# ── what an agent may read ────────────────────────────────────────────


def test_the_media_block_is_written_only_when_asked() -> None:
    observed = observation(state(element(time=79.5, paused=False)))
    assert "<media_state>" not in render_browser_state(observed, show_media_state=False)
    assert render_browser_state(observed, show_media_state=True) == (
        "<media_state>\nvideo 1 in frame main: at 01:19.5 of 03:30.8, playing.\n</media_state>"
    )


def test_unknown_media_state_is_never_written_as_no_media() -> None:
    assert render_media_state(SimpleNamespace()) == (
        "<media_state>\nThe media playback state was not measured this step.\n</media_state>"
    )
    unknown = observation(state(read_before="unknown", read_after="unknown"))
    assert "not measured" in render_media_state(unknown)
    empty = observation(state(unreachable_frames=[]))
    assert "No audio or video element on the page." in render_media_state(empty)


def test_a_comparison_is_a_measure_not_a_verdict() -> None:
    item = MediaComparison(element="main#0 video", correspondence="matched")
    assert item.recorded_time is None and item.restored_paused is None


# ── through the capability's observation (fake CDP) ──────────────────


async def _fake_capability(browser: Any, **knobs: Any) -> BrowserCapability:
    from tests._helpers.browser_archive import OwnedBrowser

    cap = BrowserCapability(**knobs)
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    await cap.setup()
    cap.apply_negotiation({"browser.screenshot"})
    return cap


async def test_every_observation_screenshot_carries_the_media_reads() -> None:
    from tests._helpers.fake_cdp import FakeBrowser

    browser = FakeBrowser()
    browser.media_answer = frame_value("main", raw())
    cap = await _fake_capability(browser)
    try:
        await cap.begin_observation(1)
        shot = await cap.resolve_fragment("browser.screenshot")
    finally:
        await cap.teardown()
    assert isinstance(shot, ScreenshotObs) and shot.media is not None
    assert (shot.media.read_before, shot.media.read_after) == ("measured", "measured")
    assert [e.label for e in shot.media.elements] == ["main#0 video"]
    assert browser.media_reads == 2, "one read just before and one just after the capture"


async def test_a_media_read_that_fails_never_fails_the_screenshot() -> None:
    from tests._helpers.fake_cdp import FakeBrowser

    browser = FakeBrowser()
    browser.media_answer = {"elements": "not a list"}
    cap = await _fake_capability(browser, media_state_timeout_s=0.2)
    try:
        await cap.begin_observation(1)
        shot = await cap.resolve_fragment("browser.screenshot")
    finally:
        await cap.teardown()
    assert isinstance(shot, ScreenshotObs) and shot.image_bytes
    assert shot.media is not None and not shot.media.known
    assert shot.media.detail == (
        "before: the media read failed: the media read returned no result; "
        "after: the media read failed: the media read returned no result"
    )


def test_an_alignment_that_raised_lets_nothing_pass() -> None:
    from spectragent.core.capabilities.browser.media import alignment_raised

    report = alignment_raised(state(element(), element(order=1)), RuntimeError("socket closed"))
    assert [r.outcome for r in report.records] == ["failed", "failed"]
    assert report.detail == "the alignment raised RuntimeError: socket closed"
    assert media_failed(MediaEvidence(alignment=report.records), time_tolerance_s=1.0)


def test_an_unreachable_frame_makes_a_compared_step_unrestorable() -> None:
    from spectragent.core.capabilities.browser.media import MediaFrameState, media_evidence

    frame = MediaFrameState(frame="main/1", rect=MediaRect(x=340, y=200, width=300, height=100))
    readable = state(element())
    blocked = state(element(), unreachable_frames=[frame])
    for recorded, restored in ((blocked, readable), (readable, blocked)):
        media = media_evidence(recorded, restored, tolerance_px=2.0, alignment=None)
        assert (media.recorded_unreachable_frames or media.restored_unreachable_frames) == [
            "main/1"
        ]
        assert _reasons(media) == ["media_mismatch"]
    assert _reasons(media_evidence(readable, readable, tolerance_px=2.0, alignment=None)) == []
    # Without a comparison (a side with no media state) nothing decides, these frames included.
    unknown = media_evidence(None, blocked, tolerance_px=2.0, alignment=None)
    assert unknown.comparisons is None and unknown.restored_unreachable_frames == ["main/1"]
    assert _reasons(unknown) == []
