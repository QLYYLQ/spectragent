"""Evidence grouping and image scope apply to suggestions and saved human labels alike."""

import pytest
from pydantic import ValidationError

from spectragent.annotation import (
    AnnotationError,
    AnnotationStore,
    LabelDraft,
    QueueItem,
    ReviewQueue,
    write_queue,
)
from tests._helpers.review_queues import evidence_queue


def scoped_queue() -> dict:
    data = evidence_queue("run", "frame.png").model_dump(mode="json")
    data["label"]["regions"]["image_roles"] = ["before"]
    return data


def test_saving_a_box_on_context_only_image_is_refused(tmp_path):
    queue = ReviewQueue.model_validate(scoped_queue())
    write_queue(tmp_path, queue)
    store = AnnotationStore(tmp_path, annotator="reviewer")
    with pytest.raises(AnnotationError, match="只供查看"):
        store.append_label(
            queue.queue_id,
            "first",
            LabelDraft.model_validate(
                {
                    "choices": ["read"],
                    "regions": [{"image": "frame", "kind": "evidence", "box": [1, 1, 10, 10]}],
                }
            ),
            queue_sha256=store.load_queue(queue.queue_id).sha256,
            image_size=lambda _: (64, 40),
        )
    assert not (tmp_path / "labels" / f"{queue.queue_id}.jsonl").exists()


def test_machine_suggestion_cannot_put_boxes_on_context_only_image():
    data = scoped_queue()
    data["items"][0]["suggestion"]["regions"][0]["image"] = "frame"
    with pytest.raises(ValidationError, match="只供查看"):
        ReviewQueue.model_validate(data)


@pytest.mark.parametrize("part", ["images", "fields", "links"])
def test_evidence_section_references_must_exist(part):
    evidence = {
        "images": {"role": "source", "source": {"kind": "observation", "run": "r", "step": 0}},
        "fields": {"text": "Source action"},
        "links": {"text": "Source trajectory", "run": "r"},
    }[part]
    with pytest.raises(ValidationError, match="未声明"):
        QueueItem.model_validate({"item_id": "one", part: [{**evidence, "section": "source"}]})


def test_trajectory_requires_a_concrete_run_and_anchor():
    with pytest.raises(ValidationError, match="run 和 step"):
        QueueItem(item_id="one", trajectory=True)


def test_duplicate_sections_are_rejected():
    with pytest.raises(ValidationError, match="不能重复"):
        QueueItem.model_validate(
            {
                "item_id": "one",
                "sections": [{"key": "source", "title": "Original"}] * 2,
            }
        )
