"""Offline diagnostics use real WAL traces and never authorize ignoring image regions."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.trace.digest import tree_digest
from spectragent.core.trace.encoding import encode_fragment
from spectragent.core.trace.models import TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.flywheel.cli import main
from spectragent.flywheel.diagnostics import DiagnosticInputError, DifferenceMethod, diagnose
from spectragent.schemas.restoration import BranchProvenance, RestorationCheck, RestorationEvidence
from spectragent.schemas.step_record import StepRecord
from tests._helpers.flywheel_traces import config
from tests._helpers.trace_builder import agent_result


def _png(text: str = "Price: 100", size: tuple[int, int] = (192, 64)) -> bytes:
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((10, 15), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _run(
    directory: Path,
    name: str,
    images: list[bytes | None],
    *,
    source: str | None = None,
    checkpoint: int = 0,
    url: str | None = "https://shop.test/item",
    terminal_step: int | None = None,
) -> Path:
    branch = (
        BranchProvenance(
            source_run_id=source,
            source_trace=source,
            checkpoint_step=checkpoint,
            branch_index=0,
            branch_group_id="same-input",
        )
        if source is not None
        else None
    )
    wal = WALWriter(directory, name)
    wal.init_run(config(task_id="price", site="shop.test", archived=False, branch=branch))
    for step, data in enumerate(images):
        fragments = []
        if data is not None:
            fragment = encode_fragment(
                step,
                ScreenshotObs(
                    image_bytes=data, viewport_width=192, viewport_height=64, source_url=url
                ),
            )
            assert fragment is not None
            fragments.append(fragment)
        check = (
            RestorationCheck(
                step=step,
                verdict="warn",
                reason="screenshot_distance",
                reasons=["screenshot_distance"],
                evidence=[
                    RestorationEvidence(
                        capability_id="browser", screenshot_distance=8, url_match=None
                    )
                ],
            )
            if source is not None and step <= checkpoint
            else None
        )
        wal.commit_step(
            step,
            StepRecord(
                step=step,
                origin=(
                    "terminal"
                    if step == terminal_step
                    else "restored"
                    if source is not None and step < checkpoint
                    else "policy"
                ),
                restoration=check,
            ),
            fragments=fragments,
        )
    wal.write_result(agent_result())
    wal.finalize(TraceStatus.COMPLETE)
    return wal.run_dir


def test_business_text_is_a_candidate_change_never_a_safe_mask(tmp_path: Path) -> None:
    record, replay = tmp_path / "record", tmp_path / "replay"
    original = _png("Price: 100")
    _run(record, "source", [original])
    first = _run(replay, "first", [original], source="source")
    second = _run(replay, "second", [_png("Price: 900")], source="source")
    before = tree_digest(tmp_path, metadata=True)
    out = tmp_path.parent / f"{tmp_path.name}-diagnostics"
    report = diagnose(record, replay, out, method=DifferenceMethod(grid_size=16))
    assert tree_digest(tmp_path, metadata=True) == before
    assert report.summary["recording_pairs_compared"] == 2
    assert report.summary["repeat_pairs_compared"] == 1
    (candidate,) = report.repeat_candidates
    assert candidate.different_pairs == 1 and candidate.regions
    assert (
        "Business text" in candidate.interpretation and "not permission" in candidate.interpretation
    )
    assert {c.recorded_check.verdict for c in report.recording_comparisons if c.recorded_check} == {
        "warn"
    }
    for replayed in (first, second):
        recorded = RunTrace(replayed).step(0)
        assert recorded is not None and recorded.restoration is not None
        assert recorded.restoration.verdict == "warn"
    compared = report.recording_comparisons[1]
    assert compared.byte_equal is False and compared.url_match is True
    assert compared.changed_pixels is not None and compared.changed_pixels > 0
    assert compared.dhash_distance is not None
    source_image = report.images[0]
    assert source_image.original is not None
    assert (out / source_image.original).read_bytes() == original
    html = (out / "index.html").read_text()
    assert "Recording versus replay" in html and "Repeat pair evidence" in html
    assert "No mask, restoration verdict or site exclusion is changed" in html
    assert "<script" not in html
    document = json.loads((out / "diagnostics.json").read_text())
    assert document["format"] == "af-replay-diagnostics-1"
    assert document["method"]["grid_size"] == 16
    # Full output is reproducible; wall clock and absolute input paths are absent.
    another = out.with_name(out.name + "-again")
    diagnose(record, replay, another, method=DifferenceMethod(grid_size=16))
    assert tree_digest(out) == tree_digest(another)


def test_one_repeat_is_not_dynamic_evidence_and_unknown_url_is_not_equal(tmp_path: Path) -> None:
    record, replay = tmp_path / "record", tmp_path / "replay"
    _run(record, "source", [_png()], url=None)
    _run(replay, "only", [_png("Price: 900"), _png("New action")], source="source", url=None)
    report = diagnose(record, replay, tmp_path / "out")
    assert len(report.recording_comparisons) == 1, (
        "post-checkpoint policy state is not an original-path comparison"
    )
    (candidate,) = report.repeat_candidates
    assert candidate.comparable_pairs == 0 and candidate.regions == []
    assert candidate.reason is not None and "only one replay" in candidate.reason
    assert report.recording_comparisons[0].url_match is None
    assert report.summary["recording_pairs_url_measured"] == 0


def test_dimension_mismatch_missing_image_and_unreached_step_are_distinct(tmp_path: Path) -> None:
    record, replay = tmp_path / "record", tmp_path / "replay"
    _run(record, "source", [_png(), _png(), _png()])
    _run(replay, "first", [_png(size=(193, 64)), None], source="source", checkpoint=2)
    _run(replay, "second", [_png(), _png()], source="source", checkpoint=2)
    report = diagnose(record, replay, tmp_path / "out")
    reasons = [c.reason or "" for c in report.recording_comparisons]
    assert any("dimensions differ" in reason for reason in reasons)
    assert any("no successful screenshot" in reason for reason in reasons)
    assert any("step was not committed" in reason for reason in reasons)
    assert report.summary["recording_pairs"] == 6
    assert report.summary["recording_pairs_compared"] == 2
    assert report.summary["repeat_pairs_compared"] == 0
    assert all(candidate.regions == [] for candidate in report.repeat_candidates)


def test_unreadable_committed_artifact_and_unmatched_sources_are_reported(tmp_path: Path) -> None:
    record, replay = tmp_path / "record", tmp_path / "replay"
    _run(record, "source", [_png()])
    damaged = _run(replay, "damaged", [_png()], source="source")
    # Locate the artifact through the reader, then simulate actual committed data loss.
    RunTrace(damaged).screenshots[0].unlink()
    _run(replay, "missing-source", [_png()], source="absent")
    _run(replay, "no-provenance", [_png()])
    broken = _run(replay, "broken", [_png()], source="source")
    (broken / "manifest.json").write_text("not json")
    report = diagnose(record, replay, tmp_path / "out")
    assert report.summary["unmatched_replays"] == 2
    assert report.inputs["replays"]["skipped"]["broken"]
    assert report.recording_comparisons[0].status == "not_comparable"
    reason = report.recording_comparisons[0].reason
    assert reason is not None and "does not read back" in reason


def test_early_terminal_observation_is_not_a_recovered_policy_step(tmp_path: Path) -> None:
    record, replay = tmp_path / "record", tmp_path / "replay"
    _run(record, "source", [_png(), _png(), _png()])
    _run(replay, "stopped", [_png(), _png()], source="source", checkpoint=2, terminal_step=1)
    report = diagnose(record, replay, tmp_path / "out")
    assert report.summary["recording_pairs_compared"] == 1
    assert report.recording_comparisons[1].reason is not None
    assert "terminal observation" in report.recording_comparisons[1].reason


def test_corrupt_image_bytes_are_preserved_and_never_compared(tmp_path: Path) -> None:
    record, replay = tmp_path / "record", tmp_path / "replay"
    _run(record, "source", [_png()])
    _run(replay, "corrupt", [b"not a decoded image"], source="source")
    out = tmp_path / "out"
    report = diagnose(record, replay, out)
    assert report.summary["recording_pairs_not_comparable"] == 1
    corrupt = report.images[1]
    assert corrupt.status == "unreadable" and corrupt.sha256
    assert corrupt.original is not None
    assert (out / corrupt.original).read_bytes() == b"not a decoded image"


def test_html_escapes_source_names_urls_and_error_text(tmp_path: Path) -> None:
    record, replay = tmp_path / "record", tmp_path / "replay"
    attack = "<script>alert(1)</script>"
    # POSIX allows angle brackets in names; slash-containing values go in provenance and URL.
    _run(record, "<source>", [_png()], url=f"https://shop.test/?q={attack}")
    _run(replay, "repeat", [_png()], source="<source>", url=f"https://shop.test/?q={attack}")
    _run(replay, "unmatched", [_png()], source=attack)
    out = tmp_path / "out"
    diagnose(record, replay, out)
    html = (out / "index.html").read_text()
    assert attack not in html and "&lt;script&gt;" in html
    assert "&lt;source&gt;" in html and "Content-Security-Policy" in html
    assert all(path.parent == out / "assets" for path in (out / "assets").iterdir())


def test_existing_and_input_nested_outputs_are_refused_before_writing(tmp_path: Path) -> None:
    record, replay = tmp_path / "record", tmp_path / "replay"
    _run(record, "source", [_png()])
    _run(replay, "repeat", [_png()], source="source")
    out = tmp_path / "out"
    out.mkdir()
    marker = out / "keep"
    marker.write_text("mine")
    with pytest.raises(DiagnosticInputError, match="already exists"):
        diagnose(record, replay, out)
    assert marker.read_text() == "mine"
    with pytest.raises(DiagnosticInputError, match="outside"):
        diagnose(record, replay, replay / "diagnostics")
    assert not (replay / "diagnostics").exists()


def test_cli_writes_artifacts_and_refuses_bad_parameters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record, replay = tmp_path / "record", tmp_path / "replay"
    _run(record, "source", [_png()])
    _run(replay, "repeat", [_png()], source="source")
    out = tmp_path / "out"
    argv = ["diagnose", "--recordings", str(record), "--replays", str(replay), "--out", str(out)]
    assert main([*argv, "--grid-size", "0"]) == 2
    assert not out.exists()
    assert main(argv) == 0
    assert str(out / "index.html") in capsys.readouterr().out
    assert main(argv) == 2
    assert "already exists" in capsys.readouterr().err
