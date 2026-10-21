"""Every queue the pipeline writes loads in the viewer's store, and every image it names resolves."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from PIL import Image

from lookahead.cli import run
from lookahead.queues import default_queue_id, frame_image, observation_image, region
from spectragent.annotation import (
    AnnotationStore,
    ArchiveImage,
    ImageSource,
    LabelDraft,
    ObservationImage,
    Region,
)
from spectragent.inspection import open_repository
from tests.test_lookahead.conftest import World
from tests.test_lookahead.fake_judge import ScriptedTransport, answers, prompt_text

FRAME_NAME = re.compile(r"frame-\d+-(before-\d+|after)\.png")


def _judge(request: dict) -> str:
    if "same_intent" in prompt_text(request):
        return answers(same_intent="yes", reasonable="yes", executed_as_intended="no")
    return answers(reasonable="no", executed_as_intended="uncertain")


@pytest.fixture
def annotation_dir(world: World, tmp_path: Path) -> Path:
    """One annotation directory holding the queue of every command that writes one."""
    directory = tmp_path / "annotations"
    common = ["--root", str(world.root), "--annotations", str(directory)]
    assert run(["stability", *common, "--out", str(tmp_path / "stability")]) == 0
    assert run(["gold-queue", *common, "--out", str(tmp_path / "gold"), "--n", "50"]) == 0
    transport = ScriptedTransport(_judge)
    code = run(
        ["judge-forks", *common, "--out", str(tmp_path / "forks")],
        transport_factory=lambda: transport,
    )
    assert code == 0
    return directory


def _image(repository, source: ImageSource) -> bytes:
    """What the viewer serves for an item image (the annotation router's own two reads)."""
    if isinstance(source, ArchiveImage):
        return repository.archive_image(source.run, source.step, source.archiver, source.name)
    data = repository.screenshot(source.run, source.step, view="browser")
    assert data is not None, f"{source.run} step {source.step} has no screenshot"
    return data


def _size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def test_every_queue_loads_and_every_image_resolves(world: World, annotation_dir: Path) -> None:
    store = AnnotationStore(annotation_dir, annotator="tester")
    listings = store.list_queues()
    assert [listing.error for listing in listings] == [None, None, None]
    prefixes = sorted(listing.queue_id.rsplit("-", 1)[0] for listing in listings)
    assert prefixes == ["fork-judge", "gold", "stability"]
    repository = open_repository(world.root)
    kinds: set[str] = set()
    names: set[str] = set()
    for listing in listings:
        assert listing.loaded is not None
        assert listing.loaded.queue.items, listing.queue_id
        for item in listing.loaded.queue.items:
            sizes = {}
            for image in item.images:
                data = _image(repository, image.source)
                sizes[image.role] = _size(data)
                kinds.add(image.source.kind)
                if isinstance(image.source, ArchiveImage):
                    assert image.source.archiver == "browser.frames"
                    names.add(image.source.name)
            for suggested in (item.suggestion.regions or []) if item.suggestion else []:
                width, height = sizes[suggested.image]
                assert suggested.box[2] <= width and suggested.box[3] <= height
    assert kinds == {"observation", "archive"}
    assert names and all(FRAME_NAME.fullmatch(name) for name in names)
    assert any(name.endswith("-after.png") for name in names)
    assert any("-before-" in name for name in names)


def test_labels_are_accepted_against_each_queue(world: World, annotation_dir: Path) -> None:
    store = AnnotationStore(annotation_dir, annotator="tester")
    repository = open_repository(world.root)
    queues = {listing.queue_id.rsplit("-", 1)[0]: listing.loaded for listing in store.list_queues()}

    def image_size(source: ImageSource) -> tuple[int, int]:
        return _size(_image(repository, source))

    forks = queues["fork-judge"]
    assert forks is not None
    item = forks.queue.item("branches/br-variant@1")
    assert item is not None and item.suggestion is not None
    line = store.append_label(
        forks.queue.queue_id,
        item.item_id,
        LabelDraft(answers=dict(item.suggestion.answers or {})),
        image_size=image_size,
    )
    assert line.verdict == "accepted" and line.answers == {
        "reasonable": "no",
        "executed_as_intended": "uncertain",
    }

    gold = queues["gold"]
    assert gold is not None
    item = gold.queue.items[0]
    line = store.append_label(
        gold.queue.queue_id,
        item.item_id,
        LabelDraft(
            choice="result_list",
            regions=[Region(image="next", kind="evidence", box=[0, 0, 32, 32])],
        ),
        image_size=image_size,
    )
    assert line.choice == "result_list" and line.verdict in {"accepted", "corrected"}

    stability = queues["stability"]
    assert stability is not None
    item = stability.queue.items[0]
    assert item.suggestion is not None and item.suggestion.choice is not None
    line = store.append_label(
        stability.queue.queue_id,
        item.item_id,
        LabelDraft(choice=item.suggestion.choice, regions=list(item.suggestion.regions or [])),
        image_size=image_size,
    )
    assert line.verdict == "accepted"


def test_queue_helpers_build_the_viewer_models() -> None:
    shot = observation_image("current", "What the agent saw", "record/rec-1", 2)
    assert shot.source == ObservationImage(kind="observation", run="record/rec-1", step=2)
    trailing = frame_image("delayed", "Later", "record/rec-1", None, "frame-4-after.png")
    assert trailing.source == ArchiveImage(
        kind="archive",
        run="record/rec-1",
        step=None,
        archiver="browser.frames",
        name="frame-4-after.png",
    )
    assert region("next", "evidence", [0, 0, 64, 32]).box == [0, 0, 64, 32]
    assert default_queue_id("gold").startswith("gold-")


def test_cli_refuses_a_root_that_is_not_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["extract", "--root", str(tmp_path / "missing"), "--out", str(tmp_path / "x.jsonl")])
    assert code == 2
    assert "is not a directory" in capsys.readouterr().err
