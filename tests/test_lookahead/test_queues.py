"""Review queues: the ``review-queue-1`` shape, checked before it is written."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lookahead.cli import run
from lookahead.queues import (
    Choice,
    LabelSpec,
    QueueItem,
    RegionKind,
    RegionLabel,
    ReviewQueue,
    SuggestedRegion,
    Suggestion,
    archive_image,
    default_queue_id,
    observation_image,
    write_queue,
)


def _item(**suggestion: object) -> QueueItem:
    return QueueItem(
        item_id="a@1",
        title="t",
        run="a",
        step=1,
        images=[
            observation_image("before", "Current screen", "a", 1),
            archive_image("frame", "Before the action", "a", 1, "browser.frames", "frame-1.png"),
        ],
        suggestion=Suggestion(source="test", **suggestion),  # type: ignore[arg-type]
    )


def _queue(item: QueueItem, *, multi: bool = False) -> ReviewQueue:
    return ReviewQueue(
        queue_id="q-1",
        title="t",
        instructions="i",
        label=LabelSpec(
            choices=[Choice(value="yes", text="Yes"), Choice(value="no", text="No")],
            multi=multi,
            regions=RegionLabel(kinds=[RegionKind(value="evidence", text="Evidence")]),
        ),
        items=[item],
    )


def test_a_queue_is_written_in_the_agreed_shape(tmp_path: Path) -> None:
    item = _item(
        choice="yes",
        regions=[SuggestedRegion(image="frame", kind="evidence", box=[0, 0, 10, 10])],
    )
    path = write_queue(tmp_path, _queue(item))
    assert path == tmp_path / "queues" / "q-1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["format"] == "review-queue-1"
    assert set(document) == {"format", "queue_id", "title", "instructions", "label", "items"}
    written = document["items"][0]
    assert set(written) == {
        "item_id",
        "title",
        "run",
        "step",
        "images",
        "fields",
        "links",
        "suggestion",
    }
    assert written["images"][1]["source"] == {
        "kind": "archive",
        "run": "a",
        "step": 1,
        "archiver": "browser.frames",
        "name": "frame-1.png",
    }
    assert set(written["suggestion"]) == {
        "choice",
        "choices",
        "regions",
        "note",
        "source",
        "rationale",
    }


def test_a_suggestion_must_name_offered_choices_kinds_and_images() -> None:
    with pytest.raises(ValidationError, match="unknown choice"):
        _queue(_item(choice="maybe"))
    with pytest.raises(ValidationError, match="kind"):
        _queue(_item(regions=[SuggestedRegion(image="frame", kind="change", box=[0, 0, 1, 1])]))
    with pytest.raises(ValidationError, match="not one of its images"):
        _item(regions=[SuggestedRegion(image="after", kind="evidence", box=[0, 0, 1, 1])])
    with pytest.raises(ValidationError, match="single-choice"):
        _queue(_item(choices=["yes"]))
    assert _queue(_item(choices=["yes", "no"]), multi=True).items


def test_queue_ids_are_file_names() -> None:
    with pytest.raises(ValidationError, match="file name"):
        ReviewQueue(queue_id="../x", title="t", instructions="i", label=LabelSpec(), items=[])
    assert default_queue_id("gold").startswith("gold-")


def test_cli_refuses_a_root_that_is_not_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["extract", "--root", str(tmp_path / "missing"), "--out", str(tmp_path / "x.jsonl")])
    assert code == 2
    assert "is not a directory" in capsys.readouterr().err
