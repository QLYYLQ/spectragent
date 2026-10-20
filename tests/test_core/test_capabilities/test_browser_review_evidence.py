"""The two sources of browser review evidence's in-viewport image fact.

The replay-log rule runs on synthetic committed traces. Each case commits one
replay step: a raw DOM document whose DOMSnapshot is written in the shape
Chrome returns (string table, per-node attribute pairs, ``currentSourceURL``
rare strings, layout bounds and scroll offsets in device pixels) and that
step's replay log. The rule reads both only through the committed reader.

The capture-state reading takes the step's recorded ``browser.capture_state``
fragment (or its absence) and says whether it measured a failed visible image.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.archive.encoding import gzip_json
from spectragent.core.capabilities.browser.observations import BrowserCaptureStateObs, DOMObs
from spectragent.core.capabilities.browser.review_evidence import (
    CAPTURE_STATE_IMAGES_NOT_COVERED,
    VIEWPORT_IMAGE_RULE_NOT_COVERED,
    BrowserEvidenceUnavailable,
    capture_state_failed_images,
    replay_images_not_served_in_viewport,
)
from spectragent.core.trace.models import TraceConfig, TraceNegotiation
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.observation.capture_state import FailedVisibleResources, PointerState
from spectragent.schemas.observation.failure import FragmentFailure
from tests._helpers.trace_builder import archive_file, step_record

PAGE = "https://shop.example.test/item/1"
LOADER = "LOADER-A"
EGGS = "https://img.example.test/eggs.webp"
OBSERVED_AT = 1000.0


def snapshot_document(
    nodes: list[dict[str, Any]], strings: list[str], *, scroll_y: float = 0.0
) -> dict[str, Any]:
    """One DOMSnapshot document; ``nodes`` items give attrs, current source and bounds."""

    def ref(value: str) -> int:
        strings.append(value)
        return len(strings) - 1

    attributes, current_index, current_value, layout_index, bounds = [], [], [], [], []
    for index, node in enumerate(nodes):
        attributes.append([ref(item) for pair in node.get("attrs", {}).items() for item in pair])
        if node.get("current"):
            current_index.append(index)
            current_value.append(ref(node["current"]))
        if node.get("bounds"):
            layout_index.append(index)
            bounds.append(list(node["bounds"]))
    return {
        "nodes": {
            "attributes": attributes,
            "currentSourceURL": {"index": current_index, "value": current_value},
        },
        "layout": {"nodeIndex": layout_index, "bounds": bounds},
        "scrollOffsetX": 0.0,
        "scrollOffsetY": scroll_y,
    }


def dom_document(
    main: list[dict[str, Any]],
    *,
    subframes: tuple[list[dict[str, Any]], ...] = (),
    scroll_y: float = 0.0,
    dpr: float = 1.0,
    loader: str = LOADER,
    url: str = PAGE,
    with_snapshot: bool = True,
) -> dict[str, Any]:
    strings: list[str] = []
    documents = [snapshot_document(main, strings, scroll_y=scroll_y)]
    documents += [snapshot_document(nodes, strings) for nodes in subframes]
    capture = (
        {"snapshot": {"strings": strings, "documents": documents}}
        if with_snapshot
        else {"raw_dom": {"tree": "page script output"}}
    )
    return {
        "url": url,
        "loader_id": loader,
        "viewport": [1280, 720],
        "device_pixel_ratio": dpr,
        "tab": "tab-1",
        "cleaner": "browser_use",
        "capture": capture,
    }


def decision(kind: str, *, url: str = EGGS, at: float = 990.0, **fields: Any) -> dict[str, Any]:
    entry = {
        "kind": kind,
        "captured_at": at,
        "cycle": 1,
        "method": "GET",
        "url": url,
        "resource_type": "Image",
        **fields,
    }
    if kind == "miss":
        entry = {"reason": "body_unavailable", "answer": "fulfilled", "status": 404, **entry}
    return entry


def replay_trace(
    tmp_path: Path, document: dict[str, Any], log: list[dict[str, Any]], *, step: int = 2
) -> RunTrace:
    wal = WALWriter(tmp_path, "replay")
    wal.init_run(
        TraceConfig(
            negotiation=TraceNegotiation(active_archivers=["browser.dom_raw", "browser.replay_log"])
        )
    )
    for at in range(step + 1):
        extra = []
        if at == step:
            extra.append(
                archive_file("browser.dom_raw", "dom.json.gz", gzip_json(document), step=at)
            )
            if log:
                extra.append(
                    archive_file(
                        "browser.replay_log",
                        "replay.jsonl",
                        "\n".join(json.dumps(entry) for entry in log).encode(),
                        step=at,
                    )
                )
        wal.commit_step(at, step_record(at), extra_files=extra)
    return RunTrace(wal.run_dir)


def rule(trace: RunTrace, *, step: int = 2, recorded: bool = True, **changes: Any):
    arguments: dict[str, Any] = {
        "replay_log_recorded": recorded,
        "url": PAGE + "#reviews",
        "loader_id": LOADER,
        "observed_at": OBSERVED_AT,
        **changes,
    }
    return replay_images_not_served_in_viewport(trace, step, **arguments)


IMAGE_IN_VIEW = {"attrs": {"class": "cover"}, "current": EGGS, "bounds": (100, 200, 300, 300)}


@pytest.mark.parametrize(
    ("node", "scroll_y", "dpr"),
    [
        (IMAGE_IN_VIEW, 0.0, 1.0),
        # The URL in a srcset attribute value, partly inside the viewport's bottom edge.
        ({"attrs": {"srcset": f"{EGGS} 2x"}, "bounds": (0, 700, 200, 200)}, 0.0, 1.0),
        # Scrolled into view: page y 2000 with the document scrolled to 1800.
        ({"attrs": {"src": EGGS}, "bounds": (0, 2000, 200, 200)}, 1800.0, 1.0),
        # Device pixels: the same element at device scale 2.
        ({"attrs": {"src": EGGS}, "bounds": (0, 4000, 400, 400)}, 3600.0, 2.0),
    ],
)
def test_image_miss_referenced_in_the_viewport_is_found(tmp_path, node, scroll_y, dpr):
    trace = replay_trace(
        tmp_path,
        dom_document([{"attrs": {"id": "root"}}, node], scroll_y=scroll_y, dpr=dpr),
        [decision("miss")],
    )
    found = rule(trace)
    assert found["measurable"] is True and found["in_viewport"] == 1
    assert found["images_not_served"] == 1 and found["referenced_in_main_document"] == 1
    assert found["listed"][0] == {
        "url": EGGS,
        "in_viewport": True,
        "referenced_in_main_document": True,
        "misses": 1,
        "served": 0,
        "statuses": [404],
        "reasons": ["body_unavailable"],
    }
    assert found["not_covered"] == list(VIEWPORT_IMAGE_RULE_NOT_COVERED)


@pytest.mark.parametrize(
    ("nodes", "log", "observed_at", "referenced"),
    [
        # Below the fold and not scrolled to.
        (
            [{"attrs": {"src": EGGS}, "bounds": (0, 2000, 200, 200)}],
            [decision("miss")],
            OBSERVED_AT,
            1,
        ),
        # A one-pixel tracker in view is not a visible image.
        ([{"attrs": {"src": EGGS}, "bounds": (10, 10, 1, 1)}], [decision("miss")], OBSERVED_AT, 1),
        # Missed thumbnails that no element of the snapshot references.
        (
            [{"attrs": {"src": "https://img.example.test/other.webp"}, "bounds": (0, 0, 99, 99)}],
            [decision("miss")],
            OBSERVED_AT,
            0,
        ),
        # Served from the archive.
        ([IMAGE_IN_VIEW], [decision("hit", status=200)], OBSERVED_AT, 0),
        # Missed only after the screenshot was taken.
        ([IMAGE_IN_VIEW], [decision("miss", at=1001.0)], OBSERVED_AT, 0),
        # Not an image request.
        ([IMAGE_IN_VIEW], [decision("miss", resource_type="XHR")], OBSERVED_AT, 0),
    ],
)
def test_misses_out_of_view_unreferenced_served_or_later_are_not_found(
    tmp_path, nodes, log, observed_at, referenced
):
    trace = replay_trace(tmp_path, dom_document(nodes), log)
    found = rule(trace, observed_at=observed_at)
    assert found["measurable"] is True and found["in_viewport"] == 0
    assert found["referenced_in_main_document"] == referenced


def test_an_answer_from_an_identical_record_counts_as_served(tmp_path):
    """The miss still decides the fact; the identical record's answer is listed as served."""
    substituted = decision(
        "substituted",
        at=995.0,
        status=200,
        record_id="eggs-cancelled",
        served_record_id="eggs-available",
        source_outcome="page_cancelled_before_response",
    )
    trace = replay_trace(tmp_path, dom_document([IMAGE_IN_VIEW]), [decision("miss"), substituted])
    found = rule(trace)
    assert found["in_viewport"] == 1
    assert (found["listed"][0]["misses"], found["listed"][0]["served"]) == (1, 1)


def test_a_folded_summary_counts_from_its_first_event(tmp_path):
    summary = decision(
        "summary",
        at=1005.0,
        folded_kind="miss",
        count=4,
        first_captured_at=995.0,
        last_captured_at=1005.0,
        reason="body_unavailable",
        answer="fulfilled",
        status=404,
    )
    trace = replay_trace(tmp_path, dom_document([IMAGE_IN_VIEW]), [summary])
    found = rule(trace)
    assert found["in_viewport"] == 1 and found["listed"][0]["misses"] == 4


def test_only_the_reviewed_steps_collection_counts(tmp_path):
    """A miss collected at an earlier step of the same document is not this step's."""
    wal = WALWriter(tmp_path, "replay")
    wal.init_run(
        TraceConfig(
            negotiation=TraceNegotiation(active_archivers=["browser.dom_raw", "browser.replay_log"])
        )
    )
    document = gzip_json(dom_document([IMAGE_IN_VIEW]))
    log = json.dumps(decision("miss", at=900.0)).encode()
    wal.commit_step(
        0,
        step_record(0),
        extra_files=[
            archive_file("browser.dom_raw", "dom.json.gz", document, step=0),
            archive_file("browser.replay_log", "replay.jsonl", log, step=0),
        ],
    )
    wal.commit_step(
        1,
        step_record(1),
        extra_files=[archive_file("browser.dom_raw", "dom.json.gz", document, step=1)],
    )
    trace = RunTrace(wal.run_dir)
    assert rule(trace, step=0)["in_viewport"] == 1
    later = rule(trace, step=1)
    assert later["measurable"] is True and later["images_not_served"] == 0


def test_subframe_references_are_counted_but_not_judged(tmp_path):
    trace = replay_trace(
        tmp_path,
        dom_document(
            [{"attrs": {"id": "frame-host"}, "bounds": (0, 0, 600, 400)}],
            subframes=([{"attrs": {"src": EGGS}, "bounds": (0, 0, 300, 300)}],),
        ),
        [decision("miss")],
    )
    found = rule(trace)
    assert found["in_viewport"] == 0 and found["referenced_only_in_subframes"] == 1


def test_unknown_is_never_zero(tmp_path):
    trace = replay_trace(tmp_path, dom_document([IMAGE_IN_VIEW]), [decision("miss")])
    assert rule(trace, recorded=False) == {
        "measurable": False,
        "reason": "the replay log was not recorded",
    }
    script_capture = replay_trace(
        tmp_path / "script", dom_document([], with_snapshot=False), [decision("miss")]
    )
    found = rule(script_capture)
    assert found["measurable"] is False and found["images_not_served"] == 1
    # Without any image miss, a capture lacking layout still measures zero.
    clean = replay_trace(tmp_path / "clean", dom_document([], with_snapshot=False), [])
    assert rule(clean)["measurable"] is True and rule(clean)["in_viewport"] == 0


def test_the_reviewed_document_must_be_one_capture(tmp_path):
    trace = replay_trace(tmp_path, dom_document([IMAGE_IN_VIEW]), [decision("miss")])
    with pytest.raises(BrowserEvidenceUnavailable):
        rule(trace, loader_id="ANOTHER-DOCUMENT")
    with pytest.raises(BrowserEvidenceUnavailable):
        rule(trace, url="https://shop.example.test/elsewhere")


def captured(failed: FailedVisibleResources) -> BrowserCaptureStateObs:
    return BrowserCaptureStateObs(
        pointer=PointerState(status="known", x=12, y=40),
        failed_visible_resources=failed,
        captured_at=OBSERVED_AT,
    )


@pytest.mark.parametrize(
    ("count", "identifiers", "failed"),
    [(0, (), False), (1, ("eggs.webp",), True), (11, ("eggs.webp", "milk.webp"), True)],
)
def test_capture_state_reads_a_measured_count(count, identifiers, failed):
    reading = capture_state_failed_images(
        captured(FailedVisibleResources(status="measured", count=count, identifiers=identifiers)),
        negotiated=True,
    )
    assert reading == {
        "measurable": True,
        "captured_at": OBSERVED_AT,
        "failed": failed,
        "failed_visible_images": count,
        "identifiers": list(identifiers),
        "not_covered": list(CAPTURE_STATE_IMAGES_NOT_COVERED),
    }


@pytest.mark.parametrize(
    ("fragment", "negotiated", "reason"),
    [
        (None, False, "the run did not negotiate capture_state"),
        (None, True, "the step recorded no capture_state"),
        (
            FragmentFailure(
                fragment_id="browser.capture_state", error="TimeoutError", detail="no answer"
            ),
            True,
            "the capture failed (TimeoutError)",
        ),
        (
            captured(FailedVisibleResources(status="unknown", detail="the probe raised")),
            True,
            "failed visible images are unknown: the probe raised",
        ),
        (
            captured(FailedVisibleResources(status="unavailable")),
            True,
            "failed visible images are unavailable",
        ),
    ],
)
def test_capture_state_unknown_is_never_zero(fragment, negotiated, reason):
    reading = capture_state_failed_images(fragment, negotiated=negotiated)
    assert reading["measurable"] is False and reading["reason"] == reason
    assert "failed" not in reading and "failed_visible_images" not in reading


def test_capture_state_of_another_fragment_type_is_corrupt():
    with pytest.raises(TypeError, match="browser.capture_state"):
        capture_state_failed_images(DOMObs(), negotiated=True)
