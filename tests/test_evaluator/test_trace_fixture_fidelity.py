"""The shared trace fixture must be a faithful model of production.

``write_trace`` is what every evaluator test reads. When it stops writing
what ``TraceWriter`` writes, the whole evaluator suite goes on passing
against a shape production never produces — which is how a hand-built
sidecar once came to point at ``000_screenshot.png`` after the real file had
been renamed ``000_browser.screenshot.png``.

The fixture now encodes with the writer's own function and commits through
the real WAL, so these tests check the property that matters: what the
fixture writes reads back, typed, exactly as a ``TraceWriter`` trace does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.observation import build_observation_model
from spectragent.schemas.step_record import StepRecord
from tests.test_evaluator.conftest import png_bytes, write_trace

FRAMES = [png_bytes((1, 2, 3)), png_bytes((9, 8, 7))]


@pytest.fixture
def trace_with_frames(tmp_path: Path) -> RunTrace:
    return RunTrace(write_trace(tmp_path, steps=2, screenshots=FRAMES))


def test_each_frame_reads_back_as_the_screenshot_it_was(trace_with_frames: RunTrace) -> None:
    for step, frame in enumerate(FRAMES):
        shot = trace_with_frames.observation_fragment(step, "browser.screenshot")
        assert isinstance(shot, ScreenshotObs)
        assert shot.image_bytes == frame
        assert trace_with_frames.screenshot(step) == frame


def test_the_frames_the_reader_lists_are_the_committed_artifacts(
    trace_with_frames: RunTrace,
) -> None:
    artifacts = [
        trace_with_frames.trace_dir / entry.fragments["browser.screenshot"].artifact
        for entry in trace_with_frames.manifest.steps
        if entry.fragments["browser.screenshot"].artifact is not None
    ]
    assert trace_with_frames.screenshots == artifacts
    assert [path.name for path in artifacts] == [
        "000_browser.screenshot.png",
        "001_browser.screenshot.png",
    ]


async def test_the_fixture_writes_what_trace_writer_writes(tmp_path: Path) -> None:
    """Same fragment through both paths: same index, same bytes."""
    shot = ScreenshotObs(image_bytes=FRAMES[0], viewport_width=1280, viewport_height=720)
    fixture_trace = RunTrace(write_trace(tmp_path / "fixture", steps=1, screenshots=[FRAMES[0]]))

    writer = TraceWriter(trace_dir=str(tmp_path / "writer"))
    await writer.on_run_start(task="t")
    observation = build_observation_model(frozenset({"browser.screenshot"}))(screenshot=shot)
    await writer.after_record_step(
        None, args=(StepRecord(step=0),), kwargs={"observation": observation}
    )
    assert writer.run_dir is not None
    writer_trace = RunTrace(writer.run_dir)

    fixture_files = fixture_trace.manifest.steps[0].fragments
    writer_files = writer_trace.manifest.steps[0].fragments
    assert fixture_files == writer_files

    files = writer_files["browser.screenshot"]
    assert files.artifact is not None
    for trace in (fixture_trace, writer_trace):
        assert (trace.trace_dir / files.artifact).read_bytes() == FRAMES[0]
    fixture_json = json.loads((fixture_trace.trace_dir / files.file).read_text())
    writer_json = json.loads((writer_trace.trace_dir / files.file).read_text())
    fixture_json.pop("captured_at")
    writer_json.pop("captured_at")
    assert fixture_json == writer_json
