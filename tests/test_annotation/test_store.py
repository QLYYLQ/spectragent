"""The annotation store: strict queues, append-only labels, current labels and verdicts."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from spectragent.annotation import (
    ADHOC_QUEUE_ID,
    AnnotationError,
    AnnotationStore,
    LabelDraft,
    Region,
    ReviewQueue,
    write_queue,
)
from tests._helpers.review_queues import evidence_queue, fork_judge_queue, questions_queue

SIZE = (64, 40)


def sizes(source) -> tuple[int, int]:
    return SIZE


@pytest.fixture
def store(tmp_path: Path) -> AnnotationStore:
    write_queue(tmp_path, fork_judge_queue("run"))
    write_queue(tmp_path, evidence_queue("run", "frame.png"))
    write_queue(tmp_path, questions_queue("run"))
    return AnnotationStore(tmp_path, annotator="ana")


def label(store: AnnotationStore, queue: str, item: str, **draft) -> dict:
    line = store.append_label(queue, item, LabelDraft(**draft), image_size=sizes)
    return json.loads(line.to_json_line())


def raw_lines(directory: Path, queue: str) -> list[dict]:
    text = (directory / "labels" / f"{queue}.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


# ── queues ────────────────────────────────────────────────────


def test_a_written_queue_loads_with_the_digest_of_its_bytes(store: AnnotationStore):
    import hashlib

    loaded = store.load_queue("fork-judge")
    data = (store.directory / "queues" / "fork-judge.json").read_bytes()
    assert loaded.sha256 == hashlib.sha256(data).hexdigest()
    assert [item.item_id for item in loaded.queue.items] == ["run/s0", "run/s1"]


def _queue_file(name: str, change) -> str:
    data = fork_judge_queue("run").model_dump(mode="json")
    data["queue_id"] = name
    change(data)
    return json.dumps(data)


@pytest.mark.parametrize(
    ("name", "change", "reason"),
    [
        ("unknown-field", lambda data: data.update(surprise=1), "surprise"),
        ("wrong-id", lambda data: data.update(queue_id="other"), "file name must be the queue id"),
        ("_reserved", lambda data: None, "reserved"),
        (
            "bad-choice",
            lambda data: data["items"][0]["suggestion"].update(choice="maybe"),
            "not one of this queue's choices",
        ),
        (
            "duplicate",
            lambda data: data["items"][1].update(item_id=data["items"][0]["item_id"]),
            "appears more than once",
        ),
        (
            "role-twice",
            lambda data: data["items"][0]["images"].append(data["items"][0]["images"][0]),
            "repeats an image role",
        ),
    ],
)
def test_a_broken_queue_is_listed_with_its_reason(tmp_path: Path, name: str, change, reason: str):
    write_queue(tmp_path, fork_judge_queue("run"))
    (tmp_path / "queues" / f"{name}.json").write_text(_queue_file(name, change))
    (tmp_path / "queues" / "syntax.json").write_text("{")
    listings = {
        listing.queue_id: listing
        for listing in AnnotationStore(tmp_path, annotator="a").list_queues()
    }
    assert listings["fork-judge"].error is None
    assert listings[name].loaded is None
    assert reason in (listings[name].error or "")
    assert "not a review queue" in (listings["syntax"].error or "")


def test_suggested_regions_must_name_the_item_images_and_queue_kinds() -> None:
    data = evidence_queue("run", "frame.png").model_dump(mode="json")
    data["items"][0]["suggestion"]["regions"][0]["image"] = "elsewhere"
    with pytest.raises(ValueError, match="not one of this item's images"):
        ReviewQueue.model_validate(data)
    data = fork_judge_queue("run").model_dump(mode="json")
    data["items"][0]["suggestion"]["regions"] = []
    with pytest.raises(ValueError, match="does not label regions"):
        ReviewQueue.model_validate(data)


@pytest.mark.parametrize("queue_id", ["../escape", "a/b", "", ".hidden", "x" * 200])
def test_queue_ids_never_become_paths_outside_the_directory(store: AnnotationStore, queue_id):
    with pytest.raises(AnnotationError):
        store.load_queue(queue_id)
    with pytest.raises(AnnotationError):
        store.labels(queue_id)


def test_the_directory_must_exist(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        AnnotationStore(tmp_path / "absent", annotator="a")
    with pytest.raises(AnnotationError, match="annotator"):
        AnnotationStore(tmp_path, annotator="  ")


# ── labels ────────────────────────────────────────────────────


def test_a_queue_label_line_has_the_contract_shape(store: AnnotationStore):
    line = label(store, "fork-judge", "run/s0", choice="reasonable", note="fine")
    assert set(line) == {
        "format",
        "queue_id",
        "queue_sha256",
        "item_id",
        "annotator",
        "at",
        "choice",
        "choices",
        "regions",
        "note",
        "verdict",
        "skipped",
    }
    assert line["format"] == "review-label-1"
    assert line["queue_sha256"] == store.load_queue("fork-judge").sha256
    assert (line["annotator"], line["choice"], line["choices"], line["regions"]) == (
        "ana",
        "reasonable",
        None,
        None,
    )
    assert line["verdict"] == "accepted"
    assert "+" in line["at"] or "-" in line["at"][10:] or line["at"].endswith("Z")
    assert raw_lines(store.directory, "fork-judge") == [line]


@pytest.mark.parametrize(
    ("draft", "verdict"),
    [
        ({"choice": "reasonable"}, "accepted"),
        ({"choice": "unreasonable"}, "corrected"),
        ({"reject": True, "note": "the click target is wrong"}, "rejected"),
        ({"skipped": True}, None),
    ],
)
def test_the_verdict_is_computed_against_the_suggestion(store, draft, verdict):
    assert label(store, "fork-judge", "run/s0", **draft)["verdict"] == verdict


def test_regions_compare_as_a_multiset_and_notes_never_count(store: AnnotationStore):
    region = {"image": "before", "kind": "evidence", "box": [1, 2, 30, 20]}
    accepted = label(store, "gold-evidence", "first", choices=["read"], regions=[region], note="x")
    assert accepted["verdict"] == "accepted"
    assert accepted["choice"] is None and accepted["choices"] == ["read"]
    moved = {**region, "box": [2, 2, 30, 20]}
    assert (
        label(store, "gold-evidence", "first", choices=["read"], regions=[moved])["verdict"]
        == "corrected"
    )
    doubled = [region, region]
    assert (
        label(store, "gold-evidence", "first", choices=["read"], regions=doubled)["verdict"]
        == "corrected"
    )
    assert label(store, "gold-evidence", "second", choices=[])["verdict"] is None


@pytest.mark.parametrize(
    ("queue", "item", "draft", "reason"),
    [
        ("fork-judge", "run/s0", {}, "choose one"),
        ("fork-judge", "run/s0", {"choice": "maybe"}, "not one of this queue's choices"),
        ("fork-judge", "run/s0", {"choices": ["reasonable"]}, "takes one choice"),
        ("gold-evidence", "first", {"choice": "read"}, "several choices"),
        (
            "fork-judge",
            "run/s0",
            {
                "choice": "reasonable",
                "regions": [{"image": "before", "kind": "x", "box": [0, 0, 1, 1]}],
            },
            "does not label regions",
        ),
        (
            "gold-evidence",
            "first",
            {"regions": [{"image": "before", "kind": "smudge", "box": [0, 0, 1, 1]}]},
            "not one of this queue's region kinds",
        ),
        (
            "gold-evidence",
            "first",
            {"regions": [{"image": "after", "kind": "evidence", "box": [0, 0, 1, 1]}]},
            "not one of this item's images",
        ),
        (
            "gold-evidence",
            "first",
            {"regions": [{"image": "before", "kind": "evidence", "box": [0, 0, 65, 10]}]},
            "leaves image",
        ),
        ("fork-judge", "run/s0", {"skipped": True, "choice": "reasonable"}, "carries no answer"),
        ("fork-judge", "run/s0", {"skipped": True, "reject": True}, "not both"),
        ("gold-evidence", "second", {"reject": True}, "no suggestion to reject"),
    ],
)
def test_labels_are_validated_against_the_queue_spec(store, queue, item, draft, reason):
    with pytest.raises(AnnotationError, match=reason):
        label(store, queue, item, **draft)
    assert not (store.directory / "labels" / f"{queue}.jsonl").exists()


def test_boxes_must_have_area_and_be_numbers(store: AnnotationStore):
    with pytest.raises(ValueError, match="0 <= x0 < x1"):
        Region(image="before", kind="evidence", box=[5, 5, 5, 9])
    for box in ([True, 0, 3, 4], ["1", 0, 3, 4], [0, 0, 3]):
        with pytest.raises(ValueError):
            Region.model_validate({"image": "before", "kind": "evidence", "box": box})
    whole = Region(image="before", kind="evidence", box=[1, 2.5, 3, 4])
    assert json.loads(whole.model_dump_json())["box"] == [1, 2.5, 3, 4]


def test_notes_are_refused_where_the_queue_takes_none(tmp_path: Path):
    spec = fork_judge_queue("run").label.model_copy(update={"note": False})
    write_queue(tmp_path, fork_judge_queue("run", label=spec.model_dump(mode="json")))
    store = AnnotationStore(tmp_path, annotator="a")
    draft = LabelDraft(choice="reasonable", note="hi")
    with pytest.raises(AnnotationError, match="does not take notes"):
        store.append_label("fork-judge", "run/s0", draft, image_size=sizes)


def test_unknown_items_are_not_labeled(store: AnnotationStore):
    with pytest.raises(FileNotFoundError, match="no item"):
        label(store, "fork-judge", "missing", choice="reasonable")


def test_the_latest_line_per_item_and_annotator_wins_and_history_is_kept(tmp_path: Path):
    write_queue(tmp_path, fork_judge_queue("run"))
    ana = AnnotationStore(tmp_path, annotator="ana")
    ben = AnnotationStore(tmp_path, annotator="ben")
    label(ana, "fork-judge", "run/s0", choice="unreasonable")
    label(ben, "fork-judge", "run/s0", choice="reasonable")
    label(ana, "fork-judge", "run/s0", choice="reasonable")
    log = ana.labels("fork-judge")
    current = log.current()
    assert current[("run/s0", "ana")].choice == "reasonable"
    assert current[("run/s0", "ben")].choice == "reasonable"
    assert [line.choice for line in log.history("run/s0")] == [
        "unreasonable",
        "reasonable",
        "reasonable",
    ]
    assert len(raw_lines(tmp_path, "fork-judge")) == 3


def test_file_order_decides_the_current_label_not_the_clock(store: AnnotationStore):
    first = label(store, "fork-judge", "run/s0", choice="unreasonable")
    second = label(store, "fork-judge", "run/s0", choice="reasonable")
    path = store.directory / "labels" / "fork-judge.jsonl"
    # A later line stamped with an earlier time (another machine's clock) still wins.
    second["at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n")
    assert store.labels("fork-judge").current()[("run/s0", "ana")].choice == "reasonable"


def test_unreadable_lines_are_reported_and_a_torn_line_never_swallows_the_next(
    store: AnnotationStore,
):
    label(store, "fork-judge", "run/s0", choice="reasonable")
    path = store.directory / "labels" / "fork-judge.jsonl"
    with path.open("a") as handle:
        handle.write('{"format": "review-label-1", "queue_id": "fork-ju')
    label(store, "fork-judge", "run/s1", choice="unreasonable")
    log = store.labels("fork-judge")
    assert [line.item_id for line in log.lines] == ["run/s0", "run/s1"]
    assert [error.line for error in log.errors] == [2]


def test_a_line_filed_under_another_queue_is_an_error(store: AnnotationStore):
    line = label(store, "fork-judge", "run/s0", choice="reasonable")
    path = store.directory / "labels" / "fork-judge.jsonl"
    path.write_text(json.dumps({**line, "queue_id": "gold-evidence"}) + "\n")
    log = store.labels("fork-judge")
    assert not log.lines and "gold-evidence" in log.errors[0].error


def test_concurrent_appends_keep_every_line_whole(store: AnnotationStore):
    def work(worker: int) -> None:
        own = AnnotationStore(store.directory, annotator=f"worker-{worker}")
        for _ in range(15):
            own.append_label(
                "fork-judge",
                "run/s0",
                LabelDraft(choice="reasonable", note="x" * 3000),
                image_size=sizes,
            )

    threads = [threading.Thread(target=work, args=(worker,)) for worker in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log = store.labels("fork-judge")
    assert not log.errors
    assert len(log.lines) == 90
    assert len(log.current()) == 6


def test_labels_record_which_version_of_the_queue_they_answered(store: AnnotationStore):
    before = label(store, "fork-judge", "run/s0", choice="reasonable")
    write_queue(store.directory, fork_judge_queue("run", title="Renamed"))
    assert store.load_queue("fork-judge").sha256 != before["queue_sha256"]
    assert (
        store.labels("fork-judge").current()[("run/s0", "ana")].queue_sha256
        == before["queue_sha256"]
    )


def test_a_labels_folder_that_leaves_the_directory_is_refused(tmp_path: Path):
    inside = tmp_path / "annotations"
    outside = tmp_path / "elsewhere"
    inside.mkdir()
    outside.mkdir()
    write_queue(inside, fork_judge_queue("run"))
    (inside / "labels").symlink_to(outside, target_is_directory=True)
    store = AnnotationStore(inside, annotator="a")
    with pytest.raises(AnnotationError, match="leaves the annotation directory"):
        label(store, "fork-judge", "run/s0", choice="reasonable")
    assert not list(outside.iterdir())


# ── ad-hoc labels ─────────────────────────────────────────────


def test_adhoc_labels_name_a_run_step_and_an_empty_one_clears(store: AnnotationStore):
    region = Region(image="screenshot", kind="problem", box=[0, 0, 10, 10])
    line = store.append_adhoc(
        "run", 2, LabelDraft(regions=[region], note="overlay"), screenshot_size=SIZE
    )
    data = json.loads(line.to_json_line())
    assert data["queue_id"] == ADHOC_QUEUE_ID
    assert (data["run"], data["step"], data["verdict"], data["skipped"]) == ("run", 2, None, False)
    assert "item_id" not in data and data["queue_sha256"] is None
    store.append_adhoc("run", 2, LabelDraft(), screenshot_size=SIZE)
    current = store.adhoc_labels().current()[("run#2", "ana")]
    assert not current.regions and not current.note
    assert len(store.adhoc_labels().lines) == 2


@pytest.mark.parametrize(
    ("draft", "size", "reason"),
    [
        ({"choice": "x"}, SIZE, "only a note and regions"),
        (
            {"regions": [{"image": "before", "kind": "problem", "box": [0, 0, 1, 1]}]},
            SIZE,
            "images",
        ),
        (
            {"regions": [{"image": "screenshot", "kind": "evidence", "box": [0, 0, 1, 1]}]},
            SIZE,
            "kinds",
        ),
        (
            {"regions": [{"image": "screenshot", "kind": "problem", "box": [0, 0, 1, 1]}]},
            None,
            "no committed screenshot",
        ),
        (
            {"regions": [{"image": "screenshot", "kind": "problem", "box": [0, 0, 1, 99]}]},
            SIZE,
            "leaves image",
        ),
    ],
)
def test_adhoc_labels_are_validated(store, draft, size, reason):
    with pytest.raises(AnnotationError, match=reason):
        store.append_adhoc("run", 0, LabelDraft(**draft), screenshot_size=size)


# ── several questions per item ────────────────────────────────


def test_each_item_asks_its_own_questions(store: AnnotationStore):
    from spectragent.annotation import asked_questions

    queue = store.load_queue("fork-questions").queue
    asked = {
        item.item_id: [q.key for q in asked_questions(queue.label, item)] for item in queue.items
    }
    assert asked == {
        "planned": ["same_intent", "reasonable"],
        "executed": ["same_intent", "reasonable", "executed_as_intended"],
        "only-reasonable": ["reasonable"],
    }


def test_a_question_label_line_carries_answers_instead_of_choices(store: AnnotationStore):
    line = label(
        store, "fork-questions", "planned", answers={"same_intent": "yes", "reasonable": "no"}
    )
    assert line["answers"] == {"same_intent": "yes", "reasonable": "no"}
    assert "choice" not in line and "choices" not in line
    assert line["verdict"] == "accepted"
    plain = label(store, "fork-judge", "run/s0", choice="reasonable")
    assert "answers" not in plain


@pytest.mark.parametrize(
    ("answers", "verdict"),
    [
        ({"same_intent": "yes", "reasonable": "no"}, "accepted"),
        ({"same_intent": "yes", "reasonable": "uncertain"}, "corrected"),
        ({"same_intent": "no", "reasonable": "no"}, "corrected"),
    ],
)
def test_the_verdict_compares_every_suggested_answer(store, answers, verdict):
    assert label(store, "fork-questions", "planned", answers=answers)["verdict"] == verdict


def test_questions_without_a_suggestion_have_no_verdict_and_skips_carry_no_answers(store):
    answers = {"same_intent": "yes", "reasonable": "yes", "executed_as_intended": "no"}
    assert label(store, "fork-questions", "executed", answers=answers)["verdict"] is None
    skipped = label(store, "fork-questions", "planned", skipped=True)
    assert skipped["answers"] == {} and skipped["verdict"] is None
    rejected = label(store, "fork-questions", "planned", reject=True, note="all wrong")
    assert rejected["answers"] == {} and rejected["verdict"] == "rejected"


@pytest.mark.parametrize(
    ("item", "draft", "reason"),
    [
        ("planned", {"answers": {"same_intent": "yes"}}, "missing ['reasonable']"),
        (
            "planned",
            {"answers": {"same_intent": "maybe", "reasonable": "no"}},
            "not one of the choices",
        ),
        (
            "planned",
            {"answers": {"same_intent": "yes", "reasonable": "no", "executed_as_intended": "yes"}},
            "does not ask question 'executed_as_intended'",
        ),
        ("only-reasonable", {"choice": "yes", "answers": {"reasonable": "yes"}}, "send 'answers'"),
        ("planned", {"skipped": True, "answers": {"reasonable": "no"}}, "carries no answer"),
    ],
)
def test_question_labels_are_validated(store, item, draft, reason):
    with pytest.raises(AnnotationError, match=re.escape(reason)):
        label(store, "fork-questions", item, **draft)


def test_answers_are_refused_by_a_single_question_queue(store):
    with pytest.raises(AnnotationError, match="asks no separate questions"):
        label(store, "fork-judge", "run/s0", choice="reasonable", answers={"x": "y"})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda data: data["label"].update(choices=[{"value": "x"}]), "not both"),
        (lambda data: data["label"].update(questions=[]), "at least one question"),
        (
            lambda data: data["label"]["questions"].append(data["label"]["questions"][0]),
            "question key must be unique",
        ),
        (lambda data: data["items"][2].update(questions=["unknown"]), "asks unknown questions"),
        (lambda data: data["items"][2].update(questions=[]), "asks none of the queue's questions"),
        (
            lambda data: data["items"][0]["suggestion"]["answers"].update(
                executed_as_intended="yes"
            ),
            "does not ask question",
        ),
        (
            lambda data: data["items"][0]["suggestion"].update(
                reasons={"executed_as_intended": "x"}
            ),
            "which this item does not ask",
        ),
    ],
)
def test_question_queues_are_checked_when_loaded(change, reason):
    data = questions_queue("run").model_dump(mode="json")
    change(data)
    with pytest.raises(ValueError, match=re.escape(reason)):
        ReviewQueue.model_validate(data)
