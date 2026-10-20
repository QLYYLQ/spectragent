"""Media alignment through a whole same-path branch, offline, in a real Chrome (§4.1, §4.14).

The capability-level half is ``test_browser_media_live.py``; here the restorer,
the environment's ``align_next_observation`` fan-out, the capture cycle, the
archive network source and the restoration check run together, the way the
metrics chain's same-path replay runs them, with no model spend:

- **The recording**: ``browser_use`` (``dom_vision``) with a scripted model on
  a local page whose video (``tests/fixtures/media/clip.webm``, see the other
  module) autoplays and toggles play / pause on a click. The model "thinks"
  before two of its replies (the scripted reply sleeps), so the video plays
  on between observations, as it did while the real model thought in task 30
  of the Xiaohongshu runs. Step 0 clicks nowhere after a pause; step 1 clicks
  the video (paused); step 2 clicks it again (playing); step 3 is done.
- **The branches**: the same recording restored to step 3 with
  ``media: align`` and with ``media: none`` (``verify: warn``, so every step
  is checked). The restorer re-executes the clicks at once: without
  alignment the video stands seconds behind the recording at steps 1-3 and
  the check fails ``media_mismatch``; with alignment every restored
  observation, the checkpoint's included, is captured at the recorded
  position and play state, and no alignment reaches the terminal observation.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.restoration import MediaEvidence
from tests._helpers import branch_driver as branch
from tests._helpers import replay_driver as driver
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

CLIP = Path(__file__).resolve().parents[2] / "fixtures" / "media" / "clip.webm"
#: Where the video sits (CSS px): clicking its middle toggles it.
VIDEO = (0, 0, 640, 360)
VIDEO_POINT = (320, 180)
#: The step a branch decides again; steps 0..2 are restored.
CHECKPOINT = 3
#: How long the scripted model "thinks" before replying at steps 0 and 1, in seconds.
THINK_S = 2.5

_PAGE = (
    "<!doctype html><title>watch</title><link rel=icon href='data:,'>"
    "<style>body{margin:0;background:#fff}</style>"
    f"<video id=v src=/clip.webm muted autoplay loop style='position:absolute;"
    f"left:{VIDEO[0]}px;top:{VIDEO[1]}px;width:{VIDEO[2]}px;height:{VIDEO[3]}px'></video>"
    "<p style='position:absolute;top:400px'>A video to summarise</p>"
    "<script>const v = document.getElementById('v');"
    "v.addEventListener('click', () => { if (v.paused) v.play(); else v.pause(); });"
    "</script>"
)


class _Pages(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    data: bytes = b""

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/watch.html":
            self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            return
        if path != "/clip.webm":
            self._send(404, b"", "text/plain")
            return
        total = len(self.data)
        start, end = 0, total - 1
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            first, _, last = header[len("bytes=") :].partition("-")
            start = int(first) if first else 0
            end = min(int(last), total - 1) if last else total - 1
        chunk = self.data[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Type", "video/webm")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return None


class _Site:
    """The page and the video on loopback, by IP literal: no name mapping needed."""

    def chrome_args(self, *, hermetic: bool = False) -> list[str]:
        return []


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    handler = type("Handler", (_Pages,), {"data": CLIP.read_bytes()})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NO_PROXY", "127.0.0.1,localhost")
        patch.setenv("no_proxy", "127.0.0.1,localhost")
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()
            httpd.server_close()


def _after(seconds: float, reply: str) -> Any:
    def think(_body: dict[str, Any]) -> str:
        time.sleep(seconds)
        return reply

    return think


RECORDING = [
    _after(THINK_S, driver.click_at(1000, 600)),
    _after(THINK_S, driver.click_at(*VIDEO_POINT)),
    driver.click_at(*VIDEO_POINT),
    driver.done("watched"),
]


@dataclass
class Branches:
    source: RunTrace
    traces: dict[str, RunTrace]


@pytest.fixture(scope="module")
def branches(origin: str, tmp_path_factory: pytest.TempPathFactory) -> Branches:
    base = tmp_path_factory.mktemp("media-branch")
    with ScriptedLLMServer(RECORDING) as llm:
        raw = driver.record_config(site=_Site(), llm=llm, trace_dir=base / "recorded")  # type: ignore[arg-type]
        source = asyncio.run(
            driver.run_task(raw, task="Watch the video", start_url=f"{origin}/watch.html")
        )
    assert llm.errors == [], llm.errors
    source.require_finished(len(RECORDING), "watched")
    traces: dict[str, RunTrace] = {}
    for media in ("align", "none"):
        with ScriptedLLMServer([driver.done("watched")]) as teacher:
            config = branch.branch_config(
                raw,
                llm=teacher,
                trace_dir=base / f"branches-{media}",
                sources_root=base / "recorded",
                step=CHECKPOINT,
                continue_steps=0,
            )
            config["agent"][0]["agent_loop"]["restorer"] = {
                "restorer_id": "prefix_replay",
                "verify": "warn",
                "media": media,
                "media_time_tolerance_s": 1.0,
                "media_seek_timeout_s": 2.0,
            }
            outcome = branch.run_branch_task(
                config, f"{source.run_dir.name}/s{CHECKPOINT}/b0", timeout=240
            )
        traces[media] = branch.require_ran(outcome)
    return Branches(source.trace, traces)


def _recorded(trace: RunTrace, step: int) -> tuple[float, bool]:
    shot = trace.observation_fragment(step, "browser.screenshot")
    media = getattr(shot, "media", None)
    assert media is not None, f"step {step} recorded no media state"
    (element,) = media.elements
    assert element.before is not None
    assert element.before.current_time is not None and element.before.paused is not None
    return element.before.current_time, element.before.paused


def _media(trace: RunTrace, step: int) -> MediaEvidence:
    record = trace.step(step)
    assert record is not None and record.restoration is not None
    (evidence,) = record.restoration.evidence
    assert evidence.media is not None
    return evidence.media


def test_the_recording_kept_the_video_playing_while_the_model_thought(branches: Branches) -> None:
    states = [_recorded(branches.source, step) for step in range(CHECKPOINT + 1)]
    (t0, p0), (t1, p1), (t2, p2), (t3, p3) = states
    assert (p0, p1, p2, p3) == (False, False, True, False)
    assert t1 - t0 > THINK_S - 0.5, "the video played on while the model thought"
    assert t2 > t1 and t3 >= t2


def test_without_alignment_the_restored_video_stands_behind_and_fails(branches: Branches) -> None:
    trace = branches.traces["none"]
    failing = {
        record.step
        for record in trace.step_records
        if record.restoration is not None and "media_mismatch" in record.restoration.reasons
    }
    assert {1, 2, 3} <= failing
    for step in (1, 2, 3):
        media = _media(trace, step)
        assert media.alignment is None, "media: none requests no alignment"
        (comparison,) = media.comparisons or []
        assert comparison.correspondence == "matched"
        assert comparison.recorded_time is not None and comparison.restored_time is not None
        assert comparison.recorded_time - comparison.restored_time > 1.0


def test_with_alignment_every_restored_observation_matches_the_recording(
    branches: Branches,
) -> None:
    trace = branches.traces["align"]
    for step in range(CHECKPOINT + 1):
        record = trace.step(step)
        assert record is not None and record.restoration is not None
        assert "media_mismatch" not in record.restoration.reasons, (step, record.restoration)
        media = _media(trace, step)
        assert media.alignment is not None
        (alignment,) = media.alignment
        assert alignment.outcome in ("aligned", "already_aligned"), alignment
        recorded_time, recorded_paused = _recorded(branches.source, step)
        assert alignment.target_time == recorded_time
        assert alignment.target_paused is recorded_paused
        expected = "pause_and_seek" if recorded_paused else "seek_and_play"
        assert alignment.outcome == "already_aligned" or alignment.action == expected
        (comparison,) = media.comparisons or []
        assert comparison.restored_paused is recorded_paused
        assert comparison.restored_time is not None
        assert abs(comparison.restored_time - recorded_time) < 0.5
    # The paused checkpoint frame is the recorded frame.
    paused = trace.step(2)
    assert paused is not None and paused.restoration is not None
    assert paused.restoration.evidence[0].screenshot_distance == 0


def test_nothing_after_the_checkpoint_is_aligned(branches: Branches) -> None:
    trace = branches.traces["align"]
    after = [record for record in trace.step_records if record.step > CHECKPOINT]
    assert after and all(record.restoration is None for record in after)
    assert [record.origin for record in after] == ["terminal"]
