"""Media playback state and its alignment, in a real headless Chrome (``media.py``, §4.4, §4.14).

The video is ``tests/fixtures/media/clip.webm``: 12 s of ffmpeg's synthetic
``testsrc2`` pattern, 160x90 at 10 fps, VP9 with a keyframe every second and
its seek index at the front, no audio, 74 KB, made with

    ffmpeg -f lavfi -i "testsrc2=duration=12:size=160x90:rate=10" -c:v libvpx-vp9 \\
        -b:v 40k -g 10 -keyint_min 10 -pix_fmt yuv420p -cues_to_front 1 -an clip.webm

No third-party media. A local server answers ``Range`` requests with 206.
``/clip.webm`` serves every byte; ``/limited.webm`` is the same file of which
only the first :data:`LIMIT_FRACTION` is ever sent — a request for any later
byte is held unanswered, the way a replay cannot serve bytes the recording
never fetched (on the site's own player such a seek reports the target in
``currentTime`` at once while ``seeking`` stays true and the frame never
changes; ``tmp/media_playback_20261020/probe/`` locally).

Covered: the screenshot records every element's state just before and after
the capture (frames, order, place, source, duration, position, paused) and a
cross-site frame as unreachable; alignment pauses and seeks to the exact
recorded position; a recorded playing source is seeked and played and the
capture follows at once; a seek beyond the served bytes fails, is undone and
fails the step; without alignment the position and paused-state differences
fail ``media_mismatch`` against the tolerance. The fake-CDP and pure halves
are ``test_browser_media.py``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.media import MediaElementState, MediaReading
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.loops.restore.checks import judge
from spectragent.schemas.restoration import RestorationEvidence, RestorationReason

pytestmark = pytest.mark.requires_chrome

CLIP = Path(__file__).resolve().parents[2] / "fixtures" / "media" / "clip.webm"
#: The share of ``/limited.webm`` that is ever sent: about 7 s of the 12. Chrome
#: reads about 5 s of a file before it reports the metadata.
LIMIT_FRACTION = 0.6

_STYLE = "<style>body{margin:0}video{position:absolute;width:320px;height:180px}</style>"


def _page(body: str) -> str:
    return f"<!doctype html><title>media</title><link rel=icon href='data:,'>{_STYLE}{body}"


#: Two videos in the main document (one paused at 3 s, one playing), a same-origin
#: iframe holding an audio element, and a cross-site iframe (another host name).
def _two_videos(cross_origin: str) -> str:
    return _page(
        "<video id=a src=/clip.webm muted preload=auto style='left:0;top:0'></video>"
        "<video id=b src=/clip.webm muted autoplay loop style='left:340px;top:0'></video>"
        "<iframe src=/inner.html style='position:absolute;left:0;top:200px;width:300px;"
        "height:100px;border:0'></iframe>"
        f"<iframe src='{cross_origin}/inner.html' style='position:absolute;left:340px;top:200px;"
        "width:300px;height:100px;border:0'></iframe>"
        "<script>const a = document.getElementById('a');"
        "a.addEventListener('loadedmetadata', () => { a.currentTime = 3; }, {once: true});"
        "</script>"
    )


_INNER = _page("<audio src=/clip.webm preload=auto></audio>")
#: One video, paused at 1 s once its metadata is in.
_ONE = _page(
    "<video id=v src=%s muted preload=auto style='left:0;top:0'></video>"
    "<script>const v = document.getElementById('v');"
    "v.addEventListener('loadedmetadata', () => { v.currentTime = 1; }, {once: true});"
    "</script>"
)


#: A cross-site frame (the same server under another host name) holding a video
#: paused at 1 s, placed at (100, 200) with a 2 px border.
def _cross_site(cross_origin: str, inner: str = "one.html") -> str:
    return _page(
        f"<iframe src='{cross_origin}/{inner}' style='position:absolute;left:100px;top:200px;"
        "width:400px;height:240px;border:2px solid #000;padding:0'></iframe>"
    )


#: A page whose main thread is held for a while, soon after it loads: a frame
#: read sent then gets no answer.
_BUSY = _page(
    "<video src=/clip.webm muted preload=auto style='left:0;top:0'></video>"
    "<script>window.addEventListener('load', () => setTimeout(() => {"
    "const end = performance.now() + 6000; while (performance.now() < end) {}"
    "}, 1500));</script>"
)
#: One video, playing from the start.
_PLAYING = _page(
    "<video id=v src=/clip.webm muted autoplay loop preload=auto style='left:0;top:0'></video>"
)


class _Media(BaseHTTPRequestHandler):
    data: bytes = b""

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        pages = {
            "/two.html": _two_videos(self.server.cross_origin),  # type: ignore[attr-defined]
            "/inner.html": _INNER,
            "/one.html": _ONE % "/clip.webm",
            "/one-limited.html": _ONE % "/limited.webm",
            "/playing.html": _PLAYING,
            "/cross.html": _cross_site(self.server.cross_origin),  # type: ignore[attr-defined]
            "/cross-busy.html": _cross_site(self.server.cross_origin, "busy.html"),  # type: ignore[attr-defined]
            "/busy.html": _BUSY,
        }
        if path in pages:
            self._send(200, pages[path].encode(), "text/html; charset=utf-8")
            return
        if path not in ("/clip.webm", "/limited.webm"):
            self._send(404, b"", "text/plain")
            return
        data = self.data
        total = len(data)
        served = int(total * LIMIT_FRACTION) if path == "/limited.webm" else total
        start, end = 0, total - 1
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            first, _, last = header[len("bytes=") :].partition("-")
            start = int(first) if first else 0
            end = int(last) if last else total - 1
        if start >= served:
            # Bytes the "recording" never fetched: held, never answered.
            time.sleep(30)
            return
        end = min(end, served - 1)
        self.send_response(206)
        self.send_header("Content-Type", "video/webm")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.wfile.write(data[start : end + 1])

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return None


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    handler = type("Handler", (_Media,), {"data": CLIP.read_bytes()})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    # The same server under another host name is another site: a cross-site frame.
    httpd.cross_origin = f"http://localhost:{port}"  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


class _Browser:
    """One capability on one page; observations numbered as an environment numbers them."""

    def __init__(self, cap: BrowserCapability) -> None:
        self.cap = cap
        self.cycle = 0

    async def screenshot(self) -> ScreenshotObs:
        self.cycle += 1
        await self.cap.begin_observation(self.cycle)
        shot = await self.cap.resolve_fragment("browser.screenshot")
        assert isinstance(shot, ScreenshotObs)
        return shot

    async def evaluate(self, expression: str) -> Any:
        reply = await self.cap.cdp.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=self.cap.default_session_id,
        )
        return reply.get("result", {}).get("value")

    async def wait_until(self, expression: str, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not await self.evaluate(expression):
            assert time.monotonic() < deadline, f"page never satisfied: {expression}"
            await asyncio.sleep(0.05)


async def _open(origin: str, page: str) -> _Browser:
    cap = BrowserCapability(settle_quiet_ms=100, settle_max_wait_s=1.0)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    await asyncio.wait_for(
        cap.setup(negotiation={"browser.screenshot"}, start_url=f"{origin}/{page}"), 60
    )
    return _Browser(cap)


def _observation(shot: ScreenshotObs) -> SimpleNamespace:
    return SimpleNamespace(screenshot=shot)


def _recorded(shot: ScreenshotObs, element: MediaElementState, reading: MediaReading) -> Any:
    """The screenshot as if recorded with *element* standing at *reading*."""
    assert shot.media is not None
    moved = element.model_copy(update={"before": reading, "after": reading})
    media = shot.media.model_copy(
        update={"elements": [moved if e.label == element.label else e for e in shot.media.elements]}
    )
    return _observation(shot.model_copy(update={"media": media}))


def _media_of(evidence: RestorationEvidence) -> Any:
    assert evidence.media is not None
    return evidence.media


async def test_the_screenshot_records_every_media_element_around_the_capture(origin: str) -> None:
    browser = await _open(origin, "two.html")
    try:
        await browser.wait_until(
            "document.getElementById('a').readyState >= 2"
            " && document.getElementById('a').currentTime === 3"
            " && !document.getElementById('b').paused"
        )
        shot = await browser.screenshot()
    finally:
        await browser.cap.teardown()

    media = shot.media
    assert media is not None
    assert (media.read_before, media.read_after) == ("measured", "measured")
    by_label = {element.label: element for element in media.elements}
    # The cross-site frame's audio is read on that frame's own session.
    assert sorted(by_label) == ["main#0 video", "main#1 video", "main/0#0 audio", "main/1#0 audio"]

    paused = by_label["main#0 video"]
    assert (paused.rect.x, paused.rect.y, paused.rect.width, paused.rect.height) == (
        0,
        0,
        320,
        180,
    )
    assert paused.source_kind == "url" and paused.url == f"{origin}/clip.webm"
    assert paused.duration == pytest.approx(12.0)
    assert not paused.live
    assert paused.before is not None and paused.after is not None
    assert paused.before.current_time == 3.0 and paused.after.current_time == 3.0
    assert paused.before.paused is True

    playing = by_label["main#1 video"]
    assert playing.rect.x == 340
    assert playing.before is not None and playing.after is not None
    assert playing.before.paused is False
    assert playing.before.current_time is not None and playing.after.current_time is not None
    # Read just before and just after the pixels: the position moves on in between.
    assert playing.after.current_time >= playing.before.current_time

    audio = by_label["main/0#0 audio"]
    assert audio.tag == "audio"
    # Placed in the top viewport: the same-origin frame's offset is added.
    assert audio.rect.y == pytest.approx(200, abs=1)

    cross = by_label["main/1#0 audio"]
    assert cross.before is not None and cross.before.paused is True
    assert media.unreachable_frames == []


async def test_alignment_pauses_and_seeks_to_the_exact_recorded_position(origin: str) -> None:
    browser = await _open(origin, "playing.html")
    try:
        await browser.wait_until("document.getElementById('v').currentTime > 0.5")
        first = await browser.screenshot()
        assert first.media is not None
        (element,) = first.media.elements
        recorded = _recorded(
            first, element, MediaReading(current_time=7.3, paused=True, ready_state=4)
        )

        await browser.cap.align_next_observation(recorded, media_seek_timeout_s=2.0)
        shot = await browser.screenshot()
        evidence = await browser.cap.verify_restoration(recorded, _observation(shot))
        # The request belonged to that one observation: the next one is not aligned.
        later = await browser.screenshot()
        untouched = await browser.cap.verify_restoration(recorded, _observation(later))
    finally:
        await browser.cap.teardown()

    assert shot.media is not None
    (aligned,) = shot.media.elements
    assert aligned.before is not None
    assert aligned.before.current_time == 7.3 and aligned.before.paused is True
    assert aligned.before.seeking is False and (aligned.before.ready_state or 0) >= 2

    media = _media_of(evidence)
    (record,) = media.alignment
    assert record.outcome == "aligned" and record.action == "pause_and_seek"
    assert record.from_paused is False and record.reached_time == 7.3
    assert record.seek_ms is not None and record.seek_ms < 2000
    (comparison,) = media.comparisons
    assert comparison.correspondence == "matched"
    assert comparison.recorded_time == comparison.restored_time == 7.3
    check = judge(
        3, [evidence], [], verify="warn", screenshot_max_distance=64, media_time_tolerance_s=1.0
    )
    assert "media_mismatch" not in check.reasons

    assert _media_of(untouched).alignment is None


async def test_a_playing_source_is_seeked_played_and_captured_at_once(origin: str) -> None:
    browser = await _open(origin, "one.html")
    try:
        await browser.wait_until(
            "document.getElementById('v').readyState >= 2"
            " && document.getElementById('v').currentTime === 1"
        )
        first = await browser.screenshot()
        assert first.media is not None
        (element,) = first.media.elements
        recorded = _recorded(
            first, element, MediaReading(current_time=6.0, paused=False, ready_state=4)
        )
        await browser.cap.align_next_observation(recorded, media_seek_timeout_s=2.0)
        shot = await browser.screenshot()
        evidence = await browser.cap.verify_restoration(recorded, _observation(shot))
    finally:
        await browser.cap.teardown()

    assert shot.media is not None
    (playing,) = shot.media.elements
    assert playing.before is not None and playing.before.paused is False
    assert playing.before.current_time is not None
    # Captured right after play started: a fraction of a second past the target.
    assert 6.0 <= playing.before.current_time < 6.5
    media = _media_of(evidence)
    (record,) = media.alignment
    assert record.outcome == "aligned" and record.action == "seek_and_play"
    assert record.from_paused is True and record.reached_paused is False
    check = judge(
        3, [evidence], [], verify="warn", screenshot_max_distance=64, media_time_tolerance_s=1.0
    )
    assert "media_mismatch" not in check.reasons


async def test_a_seek_beyond_the_served_bytes_fails_and_is_undone(origin: str) -> None:
    browser = await _open(origin, "one-limited.html")
    try:
        await browser.wait_until(
            "document.getElementById('v').readyState >= 2"
            " && document.getElementById('v').currentTime === 1"
        )
        first = await browser.screenshot()
        assert first.media is not None
        (element,) = first.media.elements
        # Within the served bytes: completes.
        within = _recorded(first, element, MediaReading(current_time=2.0, paused=True))
        await browser.cap.align_next_observation(within, media_seek_timeout_s=2.0)
        inside = await browser.screenshot()
        inside_evidence = await browser.cap.verify_restoration(within, _observation(inside))
        # Far past them: the seek never completes.
        beyond = _recorded(first, element, MediaReading(current_time=10.5, paused=True))
        await browser.cap.align_next_observation(beyond, media_seek_timeout_s=1.0)
        outside = await browser.screenshot()
        evidence = await browser.cap.verify_restoration(beyond, _observation(outside))
        state = await browser.evaluate(
            "(() => { const v = document.getElementById('v');"
            " return {t: v.currentTime, seeking: v.seeking, paused: v.paused}; })()"
        )
    finally:
        await browser.cap.teardown()

    (inside_record,) = _media_of(inside_evidence).alignment
    assert inside_record.outcome == "aligned" and inside_record.reached_time == 2.0

    (record,) = _media_of(evidence).alignment
    assert record.outcome == "failed"
    assert record.seek_ms is None
    assert record.rolled_back is True
    assert "did not complete" in record.detail
    # Put back where it stood before the alignment: 2 s, paused, no seek pending.
    assert state == {"t": 2.0, "seeking": False, "paused": True}
    assert outside.media is not None
    (after,) = outside.media.elements
    assert after.before is not None and after.before.current_time == 2.0
    check = judge(
        3, [evidence], [], verify="strict", screenshot_max_distance=64, media_time_tolerance_s=1.0
    )
    assert check.verdict == "diverged" and "media_mismatch" in check.reasons


async def test_without_alignment_position_and_paused_state_differences_fail(origin: str) -> None:
    browser = await _open(origin, "one.html")
    try:
        await browser.wait_until(
            "document.getElementById('v').readyState >= 2"
            " && document.getElementById('v').currentTime === 1"
        )
        shot = await browser.screenshot()
    finally:
        await browser.cap.teardown()
    assert shot.media is not None
    (element,) = shot.media.elements
    restored = _observation(shot)

    def check(reading: MediaReading, tolerance: float) -> list[RestorationReason]:
        recorded = _recorded(shot, element, reading)
        evidence = RestorationEvidence(
            capability_id="browser",
            media=_media_evidence(recorded, restored),
        )
        return judge(
            1,
            [evidence],
            [],
            verify="warn",
            screenshot_max_distance=64,
            media_time_tolerance_s=tolerance,
        ).reasons

    assert check(MediaReading(current_time=1.5, paused=True), 1.0) == []
    assert check(MediaReading(current_time=8.0, paused=True), 1.0) == ["media_mismatch"]
    assert check(MediaReading(current_time=8.0, paused=True), 10.0) == []
    assert check(MediaReading(current_time=1.0, paused=False), 10.0) == ["media_mismatch"]


def _media_evidence(recorded: Any, restored: Any) -> Any:
    from spectragent.core.capabilities.browser.restoration import media_measure

    return media_measure(recorded, restored, tolerance_px=2.0, alignment=None)


async def test_a_cross_site_frame_is_read_and_aligned_on_its_own_session(origin: str) -> None:
    browser = await _open(origin, "cross.html")
    try:
        await asyncio.sleep(0.5)
        first = await browser.screenshot()
        deadline = time.monotonic() + 10
        while not (
            first.media
            and first.media.elements
            and first.media.elements[0].before
            and first.media.elements[0].before.current_time == 1.0
        ):
            assert time.monotonic() < deadline, (
                f"the framed video never stood at 1 s: {first.media}"
            )
            await asyncio.sleep(0.2)
            first = await browser.screenshot()
        assert first.media is not None
        (element,) = first.media.elements
        recorded = _recorded(first, element, MediaReading(current_time=4.0, paused=True))
        await browser.cap.align_next_observation(recorded, media_seek_timeout_s=2.0)
        shot = await browser.screenshot()
        evidence = await browser.cap.verify_restoration(recorded, _observation(shot))
    finally:
        await browser.cap.teardown()

    assert element.label == "main/0#0 video"
    # Placed in the top viewport: the frame's content box starts at (102, 202).
    assert (element.rect.x, element.rect.y) == (
        pytest.approx(102, abs=1),
        pytest.approx(202, abs=1),
    )
    assert first.media.unreachable_frames == []
    media = _media_of(evidence)
    (record,) = media.alignment
    assert record.outcome == "aligned" and record.reached_time == 4.0
    assert shot.media is not None
    (aligned,) = shot.media.elements
    assert aligned.before is not None and aligned.before.current_time == 4.0
    assert media.recorded_unreachable_frames == media.restored_unreachable_frames == []
    check = judge(
        3, [evidence], [], verify="warn", screenshot_max_distance=64, media_time_tolerance_s=1.0
    )
    assert "media_mismatch" not in check.reasons


async def test_a_cross_site_frame_that_does_not_answer_is_unreachable(origin: str) -> None:
    cap = BrowserCapability(settle_quiet_ms=100, settle_max_wait_s=1.0, media_state_timeout_s=0.5)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    await asyncio.wait_for(
        cap.setup(negotiation={"browser.screenshot"}, start_url=f"{origin}/cross-busy.html"), 60
    )
    browser = _Browser(cap)
    try:
        calm = await browser.screenshot()
        await asyncio.sleep(2.0)  # the framed page now holds its main thread for 6 s
        busy = await browser.screenshot()
    finally:
        await cap.teardown()

    assert calm.media is not None and calm.media.unreachable_frames == []
    assert [e.label for e in calm.media.elements] == ["main/0#0 video"]
    assert busy.media is not None and busy.media.known, "the main frame still answered"
    assert [f.frame for f in busy.media.unreachable_frames] == ["main/0"]
    assert busy.media.unreachable_frames[0].rect.x == pytest.approx(102, abs=1)
    assert busy.media.elements == []
    evidence = RestorationEvidence(
        capability_id="browser",
        media=_media_evidence(_observation(calm), _observation(busy)),
    )
    assert evidence.media is not None
    assert evidence.media.restored_unreachable_frames == ["main/0"]
    check = judge(
        1, [evidence], [], verify="warn", screenshot_max_distance=64, media_time_tolerance_s=1.0
    )
    assert check.reasons == ["media_mismatch"]
