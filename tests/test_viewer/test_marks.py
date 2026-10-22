"""The viewer serves recorded actions as marks, the same way to the run page and the review page."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spectragent.annotation import ReviewQueue, write_queue
from spectragent.viewer import create_app
from tests.test_lookahead.traces import SIZE, StepSpec, click, frame, gradient, settled, write_run

ORIGIN = "http://127.0.0.1:8765"
WRITE = {"Origin": ORIGIN, "Content-Type": "application/json"}
IMAGE = gradient()


def _queue(run: str, other: str) -> ReviewQueue:
    image = {"kind": "observation", "run": run, "step": 0}
    data: dict[str, Any] = {
        "format": "review-queue-1",
        "queue_id": "marks",
        "title": "标记",
        "label": {
            "choices": [
                {"value": "box", "text": "要框", "regions": "required"},
                {"value": "none", "text": "不需要新信息", "regions": "forbidden"},
                {"value": "maybe", "text": "其他"},
            ],
            "regions": {"kinds": [{"value": "evidence", "text": "信息所在位置"}]},
        },
        "items": [
            {
                "item_id": "a",
                "images": [
                    {"role": "own", "source": image},
                    {
                        "role": "both",
                        "source": image,
                        "marks_from": [
                            {"run": run, "step": 0, "label": "分叉的动作"},
                            {"run": f"./{other}/", "step": 0, "label": "原运行的动作"},
                        ],
                    },
                    {"role": "plain", "source": image, "marks_from": []},
                    {
                        "role": "frame",
                        "source": {
                            "kind": "archive",
                            "run": run,
                            "step": 0,
                            "archiver": "browser.frames",
                            "name": "frame-1-before-0.png",
                        },
                    },
                ],
            }
        ],
    }
    return ReviewQueue.model_validate(data)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    traces = tmp_path / "traces"
    for name, x in (("branch", 10), ("source", 30)):
        write_run(
            traces,
            name,
            task="t",
            viewport=(SIZE[0] // 2, SIZE[1] // 2),
            steps=[
                StepSpec(
                    step=0,
                    image=IMAGE,
                    settle=settled(),
                    actions=[click(x, 5, "button", "搜索")],
                    frames=[
                        frame(
                            "before_action",
                            1,
                            image=IMAGE,
                            action_index=0,
                            action="browser.click_coordinate",
                        )
                    ],
                )
            ],
        )
    annotations = tmp_path / "annotations"
    write_queue(annotations, _queue("branch", "source"))
    return TestClient(
        create_app(traces, annotations=annotations, annotator="测试"), base_url=ORIGIN
    )


def _marks(client: TestClient, role: str) -> dict[str, Any]:
    sha = client.get("/api/review/queue", params={"queue_id": "marks"}).json()["sha256"]
    response = client.get(
        "/api/review/marks",
        params={"queue_id": "marks", "item_id": "a", "role": role, "queue_sha256": sha},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_run_page_gets_a_steps_marks_on_its_screenshot(client: TestClient) -> None:
    found = client.get("/api/marks", params={"key": "branch", "index": 0}).json()
    assert [found["width"], found["height"]] == list(SIZE)
    (mark,) = found["sets"][0]["marks"]
    assert (mark["number"], mark["kind"], mark["x"], mark["y"]) == (1, "click", 20, 10)
    assert mark["text"] == "点击 (10, 5) → 按钮“搜索”"
    assert client.get("/api/marks", params={"key": "branch", "index": 7}).status_code == 404


def test_queue_images_follow_their_source_or_name_whose_actions(client: TestClient) -> None:
    own = _marks(client, "own")
    assert [mark["x"] for mark in own["sets"][0]["marks"]] == [20]
    both = _marks(client, "both")
    assert [(entry["run"], entry["label"]) for entry in both["sets"]] == [
        ("branch", "分叉的动作"),
        ("source", "原运行的动作"),  # the run key in its one canonical spelling
    ]
    assert both["sets"][1]["marks"][0]["x"] == 60
    assert _marks(client, "plain")["sets"] == []
    frame_marks = _marks(client, "frame")
    assert [mark["number"] for mark in frame_marks["sets"][0]["marks"]] == [1]


def test_a_choice_box_rule_is_enforced_when_a_label_is_saved(client: TestClient) -> None:
    sha = client.get("/api/review/queue", params={"queue_id": "marks"}).json()["sha256"]
    box = {"image": "own", "kind": "evidence", "box": [0, 0, 10, 10]}

    def save(**draft: Any) -> Any:
        body = {"queue_id": "marks", "item_id": "a", "queue_sha256": sha, **draft}
        return client.post("/api/review/label", content=json.dumps(body), headers=WRITE)

    refused = save(choice="none", regions=[box])
    assert refused.status_code == 400
    assert refused.json()["detail"] == "选了“不需要新信息”就不能画框：请删掉框，或者换一个选项"
    missing = save(choice="box", regions=[])
    assert missing.status_code == 400 and "至少要画一个框" in missing.json()["detail"]
    assert save(choice="none", regions=[]).status_code == 200
    assert save(choice="box", regions=[box]).status_code == 200
    assert save(choice="maybe", regions=[]).status_code == 200
    # a skip carries no answer, so no rule applies
    assert save(skipped=True).status_code == 200
