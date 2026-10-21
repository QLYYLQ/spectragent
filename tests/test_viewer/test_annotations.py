"""The viewer labels into its annotation directory only, and only from its own page."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spectragent.annotation import AnnotationError
from spectragent.viewer import create_app
from tests._helpers.review_queues import (
    SCREEN_SIZE,
    build_review_run,
    evidence_queue,
    fork_judge_queue,
    png,
    save,
)

ORIGIN = "http://127.0.0.1:8765"
WRITE = {"Origin": ORIGIN, "Content-Type": "application/json"}


def snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def workspace(tmp_path: Path):
    traces = tmp_path / "traces"
    run = build_review_run(
        traces,
        frames={
            0: [("frame-0.png", png(color=(1, 2, 3)))],
            1: [("frame-1.png", png((32, 20)))],
            None: [("frame-tail.png", png((16, 10)))],
        },
    )
    annotations = tmp_path / "annotations"
    annotations.mkdir()
    save(annotations, fork_judge_queue(run.key), evidence_queue(run.key, "frame-0.png"))
    return traces, annotations, run


@pytest.fixture
def client(workspace) -> TestClient:
    traces, annotations, _ = workspace
    app = create_app(traces, annotations=annotations, annotator="ana")
    return TestClient(app, base_url=ORIGIN)


def post(client: TestClient, path: str, body: dict, headers=WRITE):
    return client.post(path, content=json.dumps(body), headers=headers)


def test_without_annotations_the_viewer_stays_read_only(workspace):
    traces, annotations, run = workspace
    client = TestClient(create_app(traces), base_url=ORIGIN)
    assert client.get("/api/workspace").json()["annotations"] is None
    assert client.get("/api/review/queues").status_code == 403
    response = post(
        client, "/api/review/label", {"queue_id": "fork-judge", "item_id": f"{run.key}/s0"}
    )
    assert response.status_code == 403
    assert "--annotations" in response.json()["detail"]
    assert post(client, "/api/review/step", {"run": run.key, "step": 0}).status_code == 403
    assert not (annotations / "labels").exists()


def test_workspace_names_the_directory_and_annotator(client, workspace):
    _, annotations, _ = workspace
    assert client.get("/api/workspace").json()["annotations"] == {
        "directory": str(annotations.resolve()),
        "annotator": "ana",
    }


def test_the_default_annotator_is_the_os_user(workspace, monkeypatch):
    from spectragent.viewer import server

    traces, annotations, _ = workspace
    monkeypatch.setattr(server.getpass, "getuser", lambda: "os-user")
    client = TestClient(create_app(traces, annotations=annotations))
    assert client.get("/api/workspace").json()["annotations"]["annotator"] == "os-user"


def test_queues_are_listed_with_progress_and_broken_queues_with_their_reason(client, workspace):
    _, annotations, _ = workspace
    (annotations / "queues" / "broken.json").write_text('{"format": "review-queue-1"}')
    listing = client.get("/api/review/queues").json()
    rows = {row["queue_id"]: row for row in listing["queues"]}
    assert rows["broken"]["progress"] is None and "label" in rows["broken"]["error"]
    assert rows["fork-judge"]["title"] == "Branched actions"
    assert rows["fork-judge"]["progress"]["total"] == 2
    assert rows["fork-judge"]["progress"]["remaining"] == 2
    assert listing["adhoc"] == {"labels": 0, "errors": 0}


def test_appending_a_label_updates_the_item_and_progress(client, workspace):
    _, annotations, run = workspace
    item = f"{run.key}/s0"
    response = post(
        client,
        "/api/review/label",
        {"queue_id": "fork-judge", "item_id": item, "choice": "unreasonable", "note": "wrong tab"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label"]["verdict"] == "corrected"
    assert body["label"]["annotator"] == "ana"
    assert body["item"]["mine"]["choice"] == "unreasonable"
    assert body["progress"]["corrected"] == 1 and body["progress"]["remaining"] == 1
    queue = client.get("/api/review/queue", params={"queue_id": "fork-judge"}).json()
    assert queue["items"][0]["mine"]["note"] == "wrong tab"
    assert queue["items"][0]["mine"]["queue_changed"] is False
    assert queue["items"][1]["mine"] is None
    lines = (annotations / "labels" / "fork-judge.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["item_id"] == item


def test_other_annotators_and_history_are_reported_separately(workspace):
    traces, annotations, run = workspace
    item = f"{run.key}/s0"
    for who, choice in [("ana", "reasonable"), ("ben", "unreasonable"), ("ana", "unreasonable")]:
        client = TestClient(
            create_app(traces, annotations=annotations, annotator=who), base_url=ORIGIN
        )
        body = {"queue_id": "fork-judge", "item_id": item, "choice": choice}
        assert post(client, "/api/review/label", body).status_code == 200
    queue = client.get("/api/review/queue", params={"queue_id": "fork-judge"}).json()
    state = queue["items"][0]
    assert state["mine"]["choice"] == "unreasonable"
    assert [(other["annotator"], other["choice"]) for other in state["others"]] == [
        ("ben", "unreasonable")
    ]
    assert state["history"] == 3
    history = client.get("/api/review/history", params={"queue_id": "fork-judge", "item_id": item})
    assert [line["choice"] for line in history.json()["labels"]] == [
        "reasonable",
        "unreasonable",
        "unreasonable",
    ]


def test_an_invalid_label_is_refused_with_its_reason(client, workspace):
    _, annotations, _ = workspace
    region = {"image": "before", "kind": "evidence", "box": [0, 0, SCREEN_SIZE[0] + 1, 5]}
    response = post(
        client,
        "/api/review/label",
        {"queue_id": "gold-evidence", "item_id": "first", "choices": ["read"], "regions": [region]},
    )
    assert response.status_code == 400
    assert "leaves image 'before' (64x40 pixels)" in response.json()["detail"]
    response = post(
        client, "/api/review/label", {"queue_id": "gold-evidence", "item_id": "first", "who": "x"}
    )
    assert response.status_code == 422
    assert not (annotations / "labels").exists()


def test_boxes_are_checked_against_the_archived_image_size(client):
    inside = {"image": "frame", "kind": "target", "box": [0, 0, 64, 40]}
    body = {"queue_id": "gold-evidence", "item_id": "first", "choices": [], "regions": [inside]}
    response = post(client, "/api/review/label", body)
    assert response.status_code == 200, response.text
    assert response.json()["label"]["verdict"] == "corrected"


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Content-Type": "application/json"}, 403),
        ({"Origin": "http://evil.example", "Content-Type": "application/json"}, 403),
        ({"Origin": "http://127.0.0.1:9999", "Content-Type": "application/json"}, 403),
        ({"Origin": ORIGIN, "Content-Type": "text/plain"}, 415),
    ],
)
def test_cross_origin_and_form_writes_are_refused(client, workspace, headers, status):
    _, annotations, run = workspace
    body = {"queue_id": "fork-judge", "item_id": f"{run.key}/s0", "choice": "reasonable"}
    assert post(client, "/api/review/label", body, headers=headers).status_code == status
    assert (
        post(client, "/api/review/step", {"run": run.key, "step": 0}, headers=headers).status_code
        == status
    )
    assert not (annotations / "labels").exists()


def test_a_host_name_that_could_be_rebound_cannot_write(workspace):
    traces, annotations, run = workspace
    app = create_app(traces, annotations=annotations, annotator="ana")
    rebound = TestClient(app, base_url="http://attacker.example:8765")
    body = {"queue_id": "fork-judge", "item_id": f"{run.key}/s0", "choice": "reasonable"}
    headers = {"Origin": "http://attacker.example:8765", "Content-Type": "application/json"}
    response = post(rebound, "/api/review/label", body, headers=headers)
    assert response.status_code == 403 and "localhost" in response.json()["detail"]
    local = TestClient(app, base_url="http://localhost:8765")
    headers = {"Origin": "http://localhost:8765", "Content-Type": "application/json"}
    assert post(local, "/api/review/label", body, headers=headers).status_code == 200


def test_item_images_resolve_observations_and_archived_frames(client, workspace):
    _, _, run = workspace
    params = {"queue_id": "gold-evidence", "item_id": "first"}
    before = client.get("/api/review/image", params={**params, "role": "before"})
    assert before.status_code == 200 and before.headers["content-type"] == "image/png"
    frame = client.get("/api/review/image", params={**params, "role": "frame"})
    assert frame.status_code == 200 and frame.content == png(color=(1, 2, 3))
    assert frame.headers["cache-control"] == "no-store"
    missing = client.get("/api/review/image", params={**params, "role": "after"})
    assert missing.status_code == 404


def _queue_with_frame(annotations: Path, run_key: str, **source) -> None:
    queue = evidence_queue(run_key, "frame-0.png").model_dump(mode="json")
    queue["items"][0]["images"][1]["source"].update(source)
    (annotations / "queues" / "gold-evidence.json").write_text(json.dumps(queue))


def test_only_raster_archive_files_are_served(client, workspace):
    _, annotations, run = workspace
    params = {"queue_id": "gold-evidence", "item_id": "first", "role": "frame"}
    _queue_with_frame(annotations, run.key, name="frames.json")
    response = client.get("/api/review/image", params=params)
    assert response.status_code == 400 and "not a raster image" in response.json()["detail"]
    _queue_with_frame(annotations, run.key, name="frame-9.png")
    assert client.get("/api/review/image", params=params).status_code == 404
    _queue_with_frame(annotations, run.key, step=None, name="frame-tail.png")
    tail = client.get("/api/review/image", params=params)
    assert tail.status_code == 200 and tail.content == png((16, 10))


@pytest.mark.parametrize("run", ["../outside", "/etc", "a/../../b"])
def test_queue_run_keys_stay_inside_the_viewer_root(client, workspace, run):
    _, annotations, _ = workspace
    _queue_with_frame(annotations, "run-review", run=run)
    response = client.get(
        "/api/review/image",
        params={"queue_id": "gold-evidence", "item_id": "first", "role": "frame"},
    )
    assert response.status_code == 400


def test_adhoc_step_labels_round_trip(client, workspace):
    _, annotations, run = workspace
    region = {"image": "screenshot", "kind": "problem", "box": [1, 1, 20, 20]}
    response = post(
        client,
        "/api/review/step",
        {"run": run.key, "step": 1, "regions": [region], "note": "popup"},
    )
    assert response.status_code == 200, response.text
    state = client.get("/api/review/step", params={"run": run.key, "step": 1}).json()
    assert state["mine"]["note"] == "popup" and state["mine"]["regions"] == [region]
    assert [kind["value"] for kind in state["spec"]["regions"]["kinds"]] == [
        "needed_information",
        "problem",
        "other",
    ]
    listing = client.get("/api/review/adhoc").json()["labels"]
    assert [(row["run"], row["step"]) for row in listing] == [(run.key, 1)]
    assert client.get("/api/review/queues").json()["adhoc"]["labels"] == 1
    cleared = post(client, "/api/review/step", {"run": run.key, "step": 1})
    assert cleared.json()["mine"] is None and len(cleared.json()["history"]) == 2
    assert client.get("/api/review/adhoc").json()["labels"] == []
    line = json.loads((annotations / "labels" / "_adhoc.jsonl").read_text().splitlines()[0])
    assert line["queue_id"] == "_adhoc" and "item_id" not in line


def test_adhoc_labels_need_a_committed_step_and_boxes_inside_its_screenshot(client, workspace):
    _, _, run = workspace
    assert post(client, "/api/review/step", {"run": run.key, "step": 9}).status_code == 404
    assert post(client, "/api/review/step", {"run": "absent", "step": 0}).status_code == 404
    outside = {"image": "screenshot", "kind": "problem", "box": [0, 0, 10, 41]}
    response = post(client, "/api/review/step", {"run": run.key, "step": 0, "regions": [outside]})
    assert response.status_code == 400


def test_labeling_never_writes_inside_the_trace_root(client, workspace):
    traces, annotations, run = workspace
    before = snapshot(traces)
    body = {"queue_id": "fork-judge", "item_id": f"{run.key}/s1", "choice": "reasonable"}
    assert post(client, "/api/review/label", body).status_code == 200
    assert (
        post(client, "/api/review/step", {"run": run.key, "step": 0, "note": "n"}).status_code
        == 200
    )
    assert snapshot(traces) == before
    assert sorted(snapshot(annotations / "labels")) == ["_adhoc.jsonl", "fork-judge.jsonl"]


def test_an_annotation_directory_inside_a_run_is_refused(workspace):
    traces, _, run = workspace
    inside = run.run_dir / "labels-here"
    inside.mkdir()
    with pytest.raises(AnnotationError, match="labels never go into a trace"):
        create_app(traces, annotations=inside)
    with pytest.raises(FileNotFoundError, match="does not exist"):
        create_app(traces, annotations=traces.parent / "absent")
