"""``browser.frames`` against a real headless Chrome (§4.14).

The first test runs the shipped recording config with ``browser.frames`` added
to ``trace_writer.archive``, end to end — the real Composer, runtime, UI-TARS
agent and trace writer — against the fixture's ``frames`` page, whose one
button paints the whole page at once. A loopback scripted model
(``tests/_helpers/scripted_llm.py``) answers:

1. after a pause longer than ``after_observation_delay_s``: click the button,
   then wait — the observation's ``after_observation`` frame is captured
   before the click, the click's ``before_action`` frame shows the page as the
   agent observed it, and the wait's ``before_action`` frame shows the painted
   page, an intermediate screen no observation captures;
2. at once: wait — the ``after_observation`` frame is superseded by the action;
3. at once: finished — UI-TARS's ``finished()`` runs ``browser.done``, an
   action like any other, so it gets its ``before_action`` frame too.

Every frame has the observation screenshot's pixel size, and everything is
read back through the pure readers over the committed trace.

The second test measures what a frame costs: the capture latency of
``before_action`` and ``after_observation`` frames, and the time an action
waits for its frame, against the same action with the archiver unbound. Both
print their numbers.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image, ImageChops

from spectragent.core.capabilities.browser.archive import FramesArchiver
from spectragent.core.capabilities.browser.archive.frames import (
    FrameRecord,
    iter_frames,
    read_frame_image,
    read_frames,
)
from spectragent.core.composer import ConfigComposer
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.registries import collect_registrars
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action
from tests._helpers.archive_bench import (
    FIXTURE_SETTLE_QUIET_MS,
    VISION_FRAGMENTS,
    LiveRecording,
)
from tests._helpers.fixture_site import (
    FRAMES_PAINT_BOX,
    FRAMES_PAINTED,
    FRAMES_PROBE,
    FixtureSite,
)
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

RECORD_CONFIG = (
    Path(__file__).resolve().parents[3] / "configs" / "flywheel" / "record_ui_tars_pinned.yaml"
)
#: The archiver's default delay, and how long the slow reply makes the run wait.
DELAY_S = 1.0
SLOW_REPLY_S = 2.0
WHITE = (255, 255, 255)


def _button_point() -> tuple[int, int]:
    left, top, width, height = FRAMES_PAINT_BOX
    return left + width // 2, top + height // 2


def _slow(text: str) -> Any:
    def reply(_body: dict[str, Any]) -> str:
        time.sleep(SLOW_REPLY_S)
        return text

    return reply


def _image(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data)) as image:
        return image.convert("RGB")


def _probe(image: Image.Image) -> tuple[int, int, int]:
    pixel = image.getpixel(FRAMES_PROBE)
    assert isinstance(pixel, tuple)
    return pixel[0], pixel[1], pixel[2]


def _same_pixels(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and ImageChops.difference(a, b).getbbox() is None


def _quantiles_ms(values: list[float]) -> dict[str, float]:
    ms = sorted(v * 1000 for v in values)
    return {"n": len(ms), "p50_ms": round(statistics.median(ms), 1), "max_ms": round(ms[-1], 1)}


def _overrides(
    raw: dict[str, Any], *, site: FixtureSite, llm: ScriptedLLMServer, trace_dir: Path
) -> None:
    agent = raw["agent"][0]
    agent["llm"].update(
        {"model": llm.model, "base_url": llm.base_url, "api_key": "scripted", "max_retries": 0}
    )
    capability = raw["environment"][0]["capabilities"][0]
    capability["settle_quiet_ms"] = FIXTURE_SETTLE_QUIET_MS
    capability["backend"]["extra_args"] = site.chrome_args()
    (writer,) = [hook for hook in raw["hook"] if hook["hook_id"] == "trace_writer"]
    writer["trace_dir"] = str(trace_dir)
    writer["archive"].append({"archiver_id": "browser.frames"})


async def test_a_recorded_run_keeps_the_screens_between_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    raw = yaml.safe_load(RECORD_CONFIG.read_text("utf-8"))
    x, y = _button_point()
    script = [
        _slow(
            "Thought: Paint the page, then let it rest.\n"
            f"Action: click(point='({x}, {y})')\n"
            "Action: wait()"
        ),
        "Thought: Wait once more.\nAction: wait()",
        "Thought: The page is painted.\nAction: finished(content='painted')",
    ]
    with FixtureSite() as site, ScriptedLLMServer(script) as llm:
        _overrides(raw, site=site, llm=llm, trace_dir=tmp_path)
        run = ConfigComposer(registrars=collect_registrars()).compose(raw)
        (writer,) = [hook for hook in run.instances("hook") if isinstance(hook, TraceWriter)]
        (frames_archiver,) = [a for a in writer.archive if isinstance(a, FramesArchiver)]
        assert frames_archiver.after_observation_delay_s == DELAY_S
        result = await AgentRuntime.for_agent(run, "main").execute(
            "Paint the page.",
            task_id="frames--0001",
            benchmark="flywheel",
            subset="fixture",
            timeout=120,
            env_context={"start_url": site.url("frames")},
        )
    assert result.answer == "painted", result
    assert llm.errors == [] and len(llm.requests) == 3
    assert writer.run_dir is not None
    trace = RunTrace(writer.run_dir)
    assert trace.manifest.status.value == "complete"
    assert trace.step_count == 3

    everything = list(iter_frames(trace))
    cycles = sorted({frame.cycle for _, frame in everything})
    assert len(cycles) == 3, everything
    by_cycle: dict[int, list[tuple[int | None, FrameRecord]]] = {
        cycle: [(step, frame) for step, frame in everything if frame.cycle == cycle]
        for cycle in cycles
    }
    first, second, third = (by_cycle[cycle] for cycle in cycles)

    # Step 0: the slow model — the after-observation frame, then one frame per action.
    assert [(step, f.kind, f.action_index, f.outcome) for step, f in first] == [
        (0, "after_observation", None, "captured"),
        (0, "before_action", 0, "captured"),
        (0, "before_action", 1, "captured"),
    ], first
    after, click, wait = (frame for _, frame in first)
    assert click.action is not None and "click" in click.action
    assert wait.action == "browser.wait"
    assert after.requested_at <= click.requested_at, "taken before the click began"
    assert after.captured_at is not None and click.captured_at is not None
    assert after.captured_at - after.requested_at < DELAY_S
    shots = trace.screenshot(0), trace.screenshot(1)
    assert shots[0] is not None and shots[1] is not None
    observed, observed_after = _image(shots[0]), _image(shots[1])
    after_image = _image(read_frame_image(trace, 0, after))
    click_image = _image(read_frame_image(trace, 0, click))
    wait_image = _image(read_frame_image(trace, 0, wait))
    for frame, image in ((after, after_image), (click, click_image), (wait, wait_image)):
        assert image.size == observed.size == (frame.width, frame.height)
    # The click's frame is the page the agent observed; the wait's, the painted page.
    assert _probe(observed) == _probe(click_image) == _probe(after_image) == WHITE
    assert _probe(wait_image) == _probe(observed_after) == FRAMES_PAINTED
    assert _same_pixels(click_image, observed), "the click's frame is the observed screen"
    assert _same_pixels(wait_image, observed_after), "nothing changed during the wait"

    # Step 1: the fast model — the action supersedes the after-observation frame.
    assert [(step, f.kind, f.outcome, f.superseded_by) for step, f in second] == [
        (1, "after_observation", "superseded", "action"),
        (1, "before_action", "captured", None),
    ], second
    assert _probe(_image(read_frame_image(trace, 1, second[1][1]))) == FRAMES_PAINTED

    # Step 2: UI-TARS's finished() is an action too (browser.done): it gets its frame.
    assert [(step, f.kind, f.action, f.outcome) for step, f in third] == [
        (2, "after_observation", None, "superseded"),
        (2, "before_action", "browser.done", "captured"),
    ], third
    assert [f.kind for f in read_frames(trace, 2)] == ["after_observation", "before_action"]

    captured = [frame for _, frame in everything if frame.outcome == "captured"]
    latency = {
        kind: _quantiles_ms(
            [
                f.captured_at - f.requested_at
                for f in captured
                if f.kind == kind and f.captured_at is not None
            ]
        )
        for kind in ("before_action", "after_observation")
    }
    print("\nbrowser.frames in a recorded run (capture latency):", latency)


async def test_what_a_frame_costs() -> None:
    """Capture latency of both kinds, and the time an action waits for its frame."""
    archiver = FramesArchiver(after_observation_delay_s=0.2)
    actions = 20
    with FixtureSite() as site:
        async with LiveRecording(
            site, archivers=(archiver,), fragments=VISION_FRAGMENTS
        ) as recording:
            cap = recording.capability
            await recording.step(site.url("frames"), scenario="frames")

            async def wait_once() -> float:
                started = time.perf_counter()
                result = await cap.execute_action(
                    Action(name="browser.wait", params={"seconds": 0})
                )
                assert result.success, result.error
                return time.perf_counter() - started

            await recording.observe()
            with_frames = [await wait_once() for _ in range(actions)]
            for _ in range(5):
                await recording.observe()
                await asyncio.sleep(0.35)
            batch = await cap.collect_archive()
            cap.bind_archivers([])
            without = [await wait_once() for _ in range(actions)]
            cap.bind_archivers([archiver])

    frames = [
        frame
        for item in batch.files
        if item.name == "frames.json"
        for frame in json.loads(item.data)["frames"]
    ]
    before = [f for f in frames if f["kind"] == "before_action"]
    after = [f for f in frames if f["kind"] == "after_observation"]
    assert len(before) == actions and all(f["outcome"] == "captured" for f in before)
    assert len(after) == 6 and [f["outcome"] for f in after] == ["superseded"] + ["captured"] * 5
    assert all(f["width"] == 1280 and f["height"] == 720 for f in before)
    report = {
        "before_action_capture": _quantiles_ms(
            [f["captured_at"] - f["requested_at"] for f in before]
        ),
        "after_observation_capture": _quantiles_ms(
            [f["captured_at"] - f["requested_at"] for f in after if f["outcome"] == "captured"]
        ),
        "action_with_frame": _quantiles_ms(with_frames),
        "action_without_frame": _quantiles_ms(without),
    }
    print("\nbrowser.frames cost (headless Chrome, 1280x720 PNG):", report)
