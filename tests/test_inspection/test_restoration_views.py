"""History/replay inspection uses committed evidence and recorded image presentation."""

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from spectragent.core.capabilities.browser.cursor import ScreenshotCursor
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.trace.encoding import encode_fragment
from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection import open_repository
from spectragent.inspection.restoration import restoration_gap, restoration_results
from spectragent.schemas.step_record import StepRecord
from spectragent.viewer import create_app


def shot(cursor):
    output = BytesIO()
    Image.new("RGB", (240, 160), "white").save(output, format="PNG")
    return ScreenshotObs(
        image_bytes=output.getvalue(),
        viewport_width=240,
        viewport_height=160,
        image_width=240,
        image_height=160,
        cursor=cursor,
    )


def encode_shot(fragment):
    encoded = encode_fragment(0, fragment)
    assert encoded is not None
    return encoded


@pytest.mark.parametrize(
    "cursor", [None, ScreenshotCursor(), ScreenshotCursor(position=(60, 40), size_css_px=48)]
)
def test_image_view_matches_model_and_preserves_capture(tmp_path, cursor):
    fragment = shot(cursor)
    wal = WALWriter(tmp_path, "run")
    wal.init_run(TraceConfig())
    wal.commit_step(0, StepRecord(step=0), fragments=[encode_shot(fragment)])
    client = TestClient(create_app(tmp_path))
    raw = client.get("/api/screenshot", params={"key": "run", "index": 0, "view": "browser"})
    model = client.get("/api/screenshot", params={"key": "run", "index": 0, "view": "agent"})
    assert raw.status_code == model.status_code == 200
    assert raw.content == fragment.image_bytes == RunTrace(wal.run_dir).screenshot(0)
    assert model.content == fragment.model_image_bytes()
    assert (model.content != raw.content) is (cursor is not None and cursor.position is not None)
    assert model.headers["content-type"] == "image/png"
    assert (
        client.get(
            "/api/screenshot", params={"key": "run", "index": 0, "view": "invented"}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/screenshot", params={"key": "run", "index": 1, "view": "agent"}
        ).status_code
        == 404
    )


def make_pair(root, history_source="source"):
    source = WALWriter(root / "record", "source")
    source.init_run(TraceConfig(loop_id="react"))
    source.commit_step(
        0,
        StepRecord.model_validate(
            {
                "step": 0,
                "llm_output": "Earlier reply",
                "actions": [{"name": "browser.click", "params": {"index": 1}}],
                "action_results": [{"success": True}],
            }
        ),
        fragments=[encode_shot(shot(ScreenshotCursor(position=(20, 20))))],
    )
    source.commit_step(1, StepRecord(step=1))
    source.finalize(TraceStatus.COMPLETE)
    branch = WALWriter(root / "branches", "branch")
    branch.init_run(
        TraceConfig.model_validate(
            {
                "loop_id": "branch",
                "branch": {
                    "source_run_id": "source",
                    "source_trace": "source",
                    "checkpoint_step": 1,
                    "branch_index": 0,
                    "branch_group_id": "g",
                },
                "resolved": {
                    "agent_loop": [
                        {
                            "loop_id": "branch",
                            "history_source": history_source,
                            "continue_steps": 1,
                            "max_steps": 20,
                            "restorer": {"restorer_id": "prefix_replay", "verify": "warn"},
                        }
                    ]
                },
            }
        )
    )
    branch.commit_step(
        0,
        StepRecord.model_validate(
            {
                "step": 0,
                "origin": "restored",
                "actions": [{"name": "browser.click", "params": {"index": 9}}],
                "action_results": [{"success": False, "error": "Actual replay failed"}],
                "source_history": {
                    "source_run_id": "source",
                    "source_step": 0,
                    "source_reply": "Earlier reply",
                    "source_actions": [{"name": "browser.click", "params": {"index": 1}}],
                },
                "restoration": {
                    "step": 0,
                    "verdict": "warn",
                    "reason": "action_failed",
                    "reasons": ["action_failed"],
                    "actions": [
                        {
                            "recorded": {"name": "browser.click", "params": {"index": 1}},
                            "executed": {"name": "browser.click", "params": {"index": 9}},
                            "method": "geometry_signature",
                            "recorded_success": True,
                            "live_success": False,
                            "result": {"success": False, "error": "Actual replay failed"},
                        }
                    ],
                    "evidence": [
                        {
                            "capability_id": "browser",
                            "url_match": True,
                            "recorded_url": "https://example.test",
                            "restored_url": "https://example.test",
                            "screenshot_distance": 2,
                        }
                    ],
                },
            }
        ),
        fragments=[encode_shot(shot(ScreenshotCursor(position=(40, 40))))],
    )
    branch.commit_step(
        1,
        StepRecord.model_validate(
            {
                "step": 1,
                "restoration": {
                    "step": 1,
                    "verdict": "ok",
                    "evidence": [
                        {
                            "capability_id": "browser",
                            "url_match": True,
                            "signature_match": True,
                            "screenshot_distance": 1,
                            "document_misses": 0,
                        }
                    ],
                },
            }
        ),
    )
    branch.commit_step(2, StepRecord(step=2))
    branch.finalize(TraceStatus.COMPLETE)
    return branch


@pytest.mark.parametrize("history_source", ["source", "restored"])
def test_history_selection_never_substitutes_source_for_actual_replay(tmp_path, history_source):
    make_pair(tmp_path, history_source)
    repo = open_repository(tmp_path)
    run = repo.run("branches/branch")
    assert run["execution"]["history_source"] == history_source
    assert run["execution"]["verify"] == "warn"
    assert run["restoration_overview"] == {
        "expected": 2,
        "checked": 2,
        "accepted": 1,
        "warned": 1,
        "diverged": 0,
        "different_steps": [0, 1],
    }
    step = repo.step("branches/branch", 0)
    assert step["comparison"]["source_key"] == "record/source"
    assert step["comparison"]["source"]["record"]["action_results"][0]["success"] is True
    assert step["record"]["action_results"][0]["success"] is False
    assert step["comparison"]["results"][0]["status"] == "changed"
    assert step["record"]["llm_output"] == ""
    assert step["gap"]["status"] == "changed"
    assert "Action 1: target or parameters changed" in step["gap"]["differences"]
    assert "Action 1: success changed" in step["gap"]["differences"]
    checkpoint = repo.step("branches/branch", 1)
    assert checkpoint["comparison"]["source_step"] == 1
    assert checkpoint["gap"]["verdict"] == "ok"
    assert checkpoint["gap"]["differences"] == ["browser: screenshot dHash distance 1"]
    assert repo.step("branches/branch", 2)["comparison"] is None


def test_source_unavailable_does_not_hide_actual_replay(tmp_path):
    branch = make_pair(tmp_path)
    single = open_repository(branch.run_dir).step(".", 0)
    assert single["comparison"]["source_status"] == "missing"
    assert single["comparison"]["source"] is None
    assert single["gap"]["differences"]
    (tmp_path / "record/source/steps/000.json").write_text("{")
    broken = open_repository(tmp_path).step("branches/branch", 0)
    assert broken["comparison"]["source_status"] == "unreadable"
    assert broken["comparison"]["error"]
    assert broken["has_screenshot"]


def test_missing_measurements_and_exceptions_are_not_successful_matches():
    assert restoration_gap({"origin": "restored"}) == {
        "status": "unmeasured",
        "differences": [],
        "unmeasured": ["Restoration check not recorded"],
        "verdict": None,
    }
    gap = restoration_gap(
        {
            "origin": "restored",
            "restoration": {
                "verdict": "warn",
                "actions": [
                    {
                        "recorded": {"name": "click"},
                        "executed": {"name": "click"},
                        "recorded_success": True,
                        "live_success": None,
                        "live_error": "TimeoutError",
                    }
                ],
            },
        }
    )
    assert gap is not None
    assert gap["differences"] == ["Action 1: execution raised an exception"]
    assert gap["unmeasured"] == ["Environment comparison not recorded"]


def test_result_comparison_keeps_exception_slot_and_detects_changed_content():
    actions = [{"name": "browser.click", "params": {"index": n}} for n in (1, 2)]
    source = {
        "actions": actions,
        "action_results": [
            {"success": True, "extracted_content": "first"},
            {"success": True, "extracted_content": "old content"},
        ],
    }
    actual = {"success": True, "extracted_content": "new content"}
    record = {
        "restoration": {
            "actions": [
                {"recorded": actions[0], "live_error": "TimeoutError", "result": None},
                {"recorded": actions[1], "result": actual},
            ]
        }
    }
    pairs = restoration_results(record, source)
    assert pairs[0]["actual"] is None
    assert pairs[0]["status"] == "unavailable"
    assert pairs[1]["source"]["extracted_content"] == "old content"
    assert pairs[1]["actual"] == actual
    assert pairs[1]["status"] == "changed"
    source["actions"] = list(reversed(actions))
    assert all(pair["status"] == "unavailable" for pair in restoration_results(record, source))


def test_result_comparison_shows_but_does_not_judge_measured_effects():
    """A re-executed wait lasts a few ms longer: its outcome still matches."""
    action = {"name": "browser.wait", "params": {"seconds": 1}}
    source = {
        "actions": [action],
        "action_results": [
            {"success": True, "effect": {"effect_id": "browser.wait", "waited_ms": 1001}}
        ],
    }
    actual = {"success": True, "effect": {"effect_id": "browser.wait", "waited_ms": 1004}}
    record = {"restoration": {"actions": [{"recorded": action, "result": actual}]}}
    (pair,) = restoration_results(record, source)
    assert pair["status"] == "matches"
    assert pair["source"]["effect"]["waited_ms"] == 1001
    assert pair["actual"]["effect"]["waited_ms"] == 1004


def test_media_differences_and_alignment_problems_are_listed_unknowns_kept_apart():
    record = {
        "step": 3,
        "origin": "restored",
        "restoration": {
            "verdict": "warn",
            "evidence": [
                {
                    "capability_id": "browser",
                    "url_match": True,
                    "signature_match": None,
                    "screenshot_distance": 4,
                    "document_misses": 0,
                    "media": {
                        "comparisons": [
                            {
                                "element": "main#0 video",
                                "correspondence": "matched",
                                "recorded_time": 73.6,
                                "restored_time": 22.6,
                                "recorded_paused": False,
                                "restored_paused": True,
                            },
                            {"element": "main#1 video", "correspondence": "extra"},
                        ],
                        "alignment": [
                            {
                                "element": "main#0 video",
                                "outcome": "failed",
                                "detail": "the seek did not complete within 2s",
                            },
                            {"element": "main#2 audio", "outcome": "aligned"},
                        ],
                    },
                }
            ],
            "actions": [],
        },
    }
    gap = restoration_gap(record)
    assert gap is not None and gap["status"] == "changed"
    media = [line for line in gap["differences"] if "media" in line]
    assert media == [
        "browser: media main#0 video at 22.600 s, recorded 73.600 s",
        "browser: media main#0 video paused state changed",
        "browser: media main#1 video extra",
        "browser: media main#0 video alignment failed (the seek did not complete within 2s)",
    ]
    record["restoration"]["evidence"][0]["media"] = {
        "comparisons": None,
        "detail": "the recorded screenshot carries no media state",
    }
    unknown = restoration_gap(record)
    assert unknown is not None
    assert (
        "browser: media state not compared (the recorded screenshot carries no media state)"
        in unknown["unmeasured"]
    )
