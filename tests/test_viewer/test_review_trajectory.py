"""A review archive exposes committed steps of its opted-in run and queue version."""

import json

import pytest
from fastapi.testclient import TestClient

import spectragent.core.capabilities.browser.actions  # noqa: F401
from spectragent.annotation import ReviewQueue, write_queue
from spectragent.schemas.step_record import ActionSpan
from spectragent.viewer import create_app
from tests.test_lookahead.traces import StepSpec, click, frame, gradient, write_run


@pytest.fixture
def review(tmp_path):
    root = tmp_path / "runs"
    picture = gradient()
    write_run(
        root,
        "recording",
        task="Read the page",
        steps=[
            StepSpec(
                0,
                image=picture,
                actions=[click(10, 10, "button", "Open")],
                action_spans=[
                    ActionSpan(
                        action="browser.click_coordinate",
                        started_at=1001,
                        ended_at=1002,
                        outcome="returned",
                    )
                ],
                llm_output="The complete recorded reply.",
            ),
            StepSpec(
                1,
                image=picture,
                frames=[
                    # Collected later, but belongs to the action in step zero.
                    frame(
                        "before_action",
                        17,
                        image=picture,
                        action_index=0,
                        action="browser.click_coordinate",
                        requested_at=1001.1,
                    ),
                    frame(
                        "before_action",
                        18,
                        outcome="timeout",
                        action_index=0,
                        action="browser.click_coordinate",
                        requested_at=1011.1,
                    ),
                ],
                action_spans=[
                    ActionSpan(
                        action="browser.click_coordinate",
                        started_at=1011,
                        ended_at=1012,
                        outcome="returned",
                    )
                ],
            ),
            StepSpec(
                2,
                image=picture,
                actions=[click(20, 30, "button", "Next")],
                action_spans=[
                    ActionSpan(
                        action="browser.click_coordinate",
                        started_at=1021,
                        ended_at=1022,
                        outcome="returned",
                    )
                ],
                frames=[
                    frame(
                        "before_action",
                        19,
                        image=gradient(size=(256, 192)),
                        action_index=0,
                        action="browser.click_coordinate",
                        requested_at=1021.1,
                    )
                ],
            ),
            StepSpec(3, image=picture, origin="terminal"),
        ],
        viewport=(64, 48),
    )
    queue = ReviewQueue.model_validate(
        {
            "format": "review-queue-1",
            "queue_id": "evidence",
            "label": {"choices": [{"value": "yes"}]},
            "items": [
                {"item_id": "enabled", "run": "recording", "step": 1, "trajectory": True},
                {"item_id": "disabled", "run": "recording", "step": 1},
            ],
        }
    )
    annotations = tmp_path / "annotations"
    write_queue(annotations, queue)
    client = TestClient(create_app(root, annotations=annotations), base_url="http://127.0.0.1")
    digest = client.get("/api/review/queue", params={"queue_id": "evidence"}).json()["sha256"]
    return (
        client,
        annotations,
        {"queue_id": "evidence", "item_id": "enabled", "queue_sha256": digest},
    )


def test_archive_contains_all_committed_steps_and_keeps_review_anchor(review):
    client, _, params = review
    response = client.get("/api/review/trajectory", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["anchor_step"] == 1
    assert [step["index"] for step in body["steps"]] == [0, 1, 2, 3]
    assert body["steps"][0]["record"]["llm_output"] == "The complete recorded reply."
    assert body["steps"][2]["record"]["actions"][0]["params"]["coordinate_x"] == 20
    assert body["steps"][3]["origin"] == "terminal"


def test_frames_follow_actual_calls_not_collection_step(review):
    client, _, params = review
    detail = client.get("/api/review/trajectory-step", params={**params, "index": 0})
    assert detail.status_code == 200, detail.text
    frames = detail.json()["frames"]
    assert len(frames) == 1 and frames[0]["id"] == "1:17:0"
    mark = frames[0]["marks"]["sets"][0]["marks"][0]
    assert (mark["x"], mark["y"], mark["number"]) == (20, 20, 1)
    # The later collection step had no corresponding recorded action. The
    # marker belongs to the call the frame actually preceded in step zero.
    assert frames[0]["marks"]["sets"][0]["step"] == 0
    image = client.get(
        "/api/review/trajectory-image",
        params={
            **params,
            "index": 0,
            "kind": "before_action",
            "frame_id": frames[0]["id"],
        },
    )
    assert image.status_code == 200 and image.content == gradient()
    wrong_step = client.get(
        "/api/review/trajectory-image",
        params={
            **params,
            "index": 1,
            "kind": "before_action",
            "frame_id": frames[0]["id"],
        },
    )
    assert wrong_step.status_code == 404
    missing = client.get("/api/review/trajectory-step", params={**params, "index": 1}).json()
    assert missing["frames"][0]["outcome"] == "timeout"
    assert missing["frames"][0]["has_image"] is False


@pytest.mark.parametrize(
    "path,extra",
    [
        ("trajectory", {}),
        ("trajectory-step", {"index": 0}),
        ("trajectory-image", {"index": 0, "kind": "observation"}),
    ],
)
def test_history_requires_opt_in_and_matching_queue_version(review, path, extra):
    client, annotations, params = review
    disabled = client.get(f"/api/review/{path}", params={**params, **extra, "item_id": "disabled"})
    assert disabled.status_code == 404
    file = annotations / "queues" / "evidence.json"
    data = json.loads(file.read_text())
    data["title"] = "Regenerated"
    file.write_text(json.dumps(data))
    stale = client.get(f"/api/review/{path}", params={**params, **extra})
    assert stale.status_code == 409


@pytest.mark.parametrize(
    "path,extra",
    [
        ("trajectory-step", {}),
        ("trajectory-image", {"kind": "observation"}),
        ("trajectory-image", {"kind": "before_action", "frame_id": "1:17:0"}),
    ],
)
def test_archive_never_serves_uncommitted_steps(review, path, extra):
    client, _, params = review
    response = client.get(f"/api/review/{path}", params={**params, **extra, "index": 99})
    assert response.status_code == 404


def test_future_actions_observations_and_frames_are_available_with_independent_geometry(review):
    client, _, params = review
    detail = client.get("/api/review/trajectory-step", params={**params, "index": 2})
    assert detail.status_code == 200, detail.text
    body = detail.json()
    observation = body["observation_marks"]
    assert (observation["width"], observation["height"]) == (128, 96)
    mark = observation["sets"][0]["marks"][0]
    assert (mark["x"], mark["y"]) == (40, 60)
    frame_data = body["frames"][0]
    marks = frame_data["marks"]
    assert (marks["width"], marks["height"]) == (256, 192)
    mark = marks["sets"][0]["marks"][0]
    assert (mark["x"], mark["y"]) == (80, 120)
    for kind, frame_id, expected in [
        ("observation", None, gradient()),
        ("before_action", frame_data["id"], gradient(size=(256, 192))),
    ]:
        image = client.get(
            "/api/review/trajectory-image",
            params={
                **params,
                "index": 2,
                "kind": kind,
                **({"frame_id": frame_id} if frame_id else {}),
            },
        )
        assert image.status_code == 200 and image.content == expected


def test_observation_pointer_is_independent_of_action_target_and_frame_pointer(review):
    client, _, params = review
    body = client.get("/api/review/trajectory-step", params={**params, "index": 2}).json()
    pointer = body["observation_pointer"]
    assert (pointer["status"], pointer["x"], pointer["y"]) == ("known", 10, 10)
    assert (pointer["viewport_x"], pointer["viewport_y"]) == (5, 5)
    assert pointer["source"] == "capture_state"
    before = body["frames"][0]["pointer"]
    assert before["status"] == "unknown" and before["x"] is None and before["y"] is None
    assert "没有独立记录" in before["problem"]
