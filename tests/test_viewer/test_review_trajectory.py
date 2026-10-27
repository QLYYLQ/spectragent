"""A review history stays inside its item, step boundary and queue version."""

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
            StepSpec(2, image=picture, origin="terminal"),
        ],
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


def test_history_contains_committed_prefix_and_full_record(review):
    client, _, params = review
    response = client.get("/api/review/trajectory", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["anchor_step"] == 1
    assert [step["index"] for step in body["steps"]] == [0, 1]
    assert body["steps"][0]["record"]["llm_output"] == "The complete recorded reply."


def test_frames_follow_actual_calls_not_collection_step(review):
    client, _, params = review
    detail = client.get("/api/review/trajectory-step", params={**params, "index": 0})
    assert detail.status_code == 200, detail.text
    frames = detail.json()["frames"]
    assert len(frames) == 1 and frames[0]["id"] == "1:17:0"
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
def test_review_never_serves_steps_after_its_anchor(review, path, extra):
    client, _, params = review
    response = client.get(f"/api/review/{path}", params={**params, **extra, "index": 2})
    assert response.status_code == 404
