"""The shared trace fixture must be a faithful model of production.

``write_trace`` is what every evaluator test reads, and its docstring
promises screenshots are "persisted the way ``ScreenshotObs`` does it".
When that stops being true the whole evaluator suite goes on passing
against a shape production never writes — which is how the sidecar's
``artifact_file`` came to point at ``000_screenshot.png`` after the real
file was renamed to ``000_browser.screenshot.png``.

Nothing in the package follows that pointer yet, so no other test can
see it. This one compares the fixture against the writer's own rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.trace.reader import RunTrace
from tests.test_evaluator.conftest import png_bytes, write_trace


@pytest.fixture
def trace_with_frames(tmp_path: Path) -> RunTrace:
    directory = write_trace(
        tmp_path,
        steps=2,
        screenshots=[png_bytes((1, 2, 3)), png_bytes((9, 8, 7))],
    )
    return RunTrace(directory)


def test_the_screenshot_sidecar_points_at_a_file_that_exists(
    trace_with_frames: RunTrace,
) -> None:
    observations = trace_with_frames.trace_dir / "observations"
    for step in range(2):
        sidecar = trace_with_frames.get_observation(step, "browser.screenshot")
        assert isinstance(sidecar, dict)
        pointer = sidecar["artifact_file"]
        assert (observations / pointer).is_file(), (
            f"step {step} sidecar points at {pointer!r}, which the fixture "
            f"never wrote — it wrote "
            f"{sorted(p.name for p in observations.iterdir())}"
        )


def test_the_pointer_is_the_name_the_trace_writer_would_have_used(
    trace_with_frames: RunTrace,
) -> None:
    """``TraceWriter`` builds it as ``NNN_<fragment_id><suffix>``.

    Spelled from ``__registry_key__`` here for the same reason the writer
    reads ``fragment_id``: a literal is what drifts.
    """
    for step in range(2):
        sidecar = trace_with_frames.get_observation(step, "browser.screenshot")
        assert isinstance(sidecar, dict)
        expected = f"{step:03d}_{ScreenshotObs.__registry_key__}.png"
        assert sidecar["artifact_file"] == expected
        assert sidecar["fragment_id"] == ScreenshotObs.__registry_key__


def test_the_frames_the_reader_finds_are_the_ones_the_pointers_name(
    trace_with_frames: RunTrace,
) -> None:
    """``RunTrace.screenshots`` globs; the sidecars point. They must agree."""
    globbed = {path.name for path in trace_with_frames.screenshots}
    pointed: set[str] = set()
    for step in range(2):
        sidecar = trace_with_frames.get_observation(step, "browser.screenshot")
        assert isinstance(sidecar, dict)
        pointed.add(sidecar["artifact_file"])
    assert globbed == pointed
