"""The annotation store: strict queues, append-only labels, current labels and verdicts."""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path

import pytest

from spectragent.annotation import (
    ADHOC_QUEUE_ID,
    AnnotationError,
    AnnotationStore,
    LabelDraft,
    QueueChangedError,
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
    loaded = store.load_queue(queue).sha256
    line = store.append_label(
        queue, item, LabelDraft(**draft), queue_sha256=loaded, image_size=sizes
    )
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
        ("wrong-id", lambda data: data.update(queue_id="other"), "文件名必须就是队列 id"),
        ("_reserved", lambda data: None, "保留"),
        (
            "bad-choice",
            lambda data: data["items"][0]["suggestion"].update(choice="maybe"),
            "不是这个队列的选项",
        ),
        (
            "duplicate",
            lambda data: data["items"][1].update(item_id=data["items"][0]["item_id"]),
            "出现了不止一次",
        ),
        (
            "role-twice",
            lambda data: data["items"][0]["images"].append(data["items"][0]["images"][0]),
            "有重复的图片角色",
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
    assert "不是有效的审阅队列" in (listings["syntax"].error or "")


def test_suggested_regions_must_name_the_item_images_and_queue_kinds() -> None:
    data = evidence_queue("run", "frame.png").model_dump(mode="json")
    data["items"][0]["suggestion"]["regions"][0]["image"] = "elsewhere"
    with pytest.raises(ValueError, match="不是这个条目的图片"):
        ReviewQueue.model_validate(data)
    data = fork_judge_queue("run").model_dump(mode="json")
    data["items"][0]["suggestion"]["regions"] = []
    with pytest.raises(ValueError, match="这个队列不画框"):
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
    with pytest.raises(AnnotationError, match="标注者名字"):
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
        ("fork-judge", "run/s0", {}, "请选一个答案"),
        ("fork-judge", "run/s0", {"choice": "maybe"}, "不是这个队列的选项"),
        ("fork-judge", "run/s0", {"choices": ["reasonable"]}, "只能单选"),
        ("gold-evidence", "first", {"choice": "read"}, "可以多选"),
        (
            "fork-judge",
            "run/s0",
            {
                "choice": "reasonable",
                "regions": [{"image": "before", "kind": "x", "box": [0, 0, 1, 1]}],
            },
            "这个队列不画框",
        ),
        (
            "gold-evidence",
            "first",
            {"regions": [{"image": "before", "kind": "smudge", "box": [0, 0, 1, 1]}]},
            "不是这个队列的框类别",
        ),
        (
            "gold-evidence",
            "first",
            {"regions": [{"image": "after", "kind": "evidence", "box": [0, 0, 1, 1]}]},
            "不是这个条目的图片",
        ),
        (
            "gold-evidence",
            "first",
            {"regions": [{"image": "before", "kind": "evidence", "box": [0, 0, 65, 10]}]},
            "超出了图片",
        ),
        ("fork-judge", "run/s0", {"skipped": True, "choice": "reasonable"}, "不带答案"),
        ("fork-judge", "run/s0", {"skipped": True, "reject": True}, "只能选一个"),
        ("gold-evidence", "second", {"reject": True}, "没有可以驳回的建议"),
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
    with pytest.raises(AnnotationError, match="不收备注"):
        loaded = store.load_queue("fork-judge").sha256
        store.append_label("fork-judge", "run/s0", draft, queue_sha256=loaded, image_size=sizes)


def test_unknown_items_are_not_labeled(store: AnnotationStore):
    with pytest.raises(FileNotFoundError, match="没有条目"):
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
                queue_sha256=own.load_queue("fork-judge").sha256,
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
    with pytest.raises(AnnotationError, match="超出了标注目录"):
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
        ({"choice": "x"}, SIZE, "只能写备注和画框"),
        (
            {"regions": [{"image": "before", "kind": "problem", "box": [0, 0, 1, 1]}]},
            SIZE,
            "不是这个条目的图片",
        ),
        (
            {"regions": [{"image": "screenshot", "kind": "evidence", "box": [0, 0, 1, 1]}]},
            SIZE,
            "框类别",
        ),
        (
            {"regions": [{"image": "screenshot", "kind": "problem", "box": [0, 0, 1, 1]}]},
            None,
            "没有截图",
        ),
        (
            {"regions": [{"image": "screenshot", "kind": "problem", "box": [0, 0, 1, 99]}]},
            SIZE,
            "超出了图片",
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
        ("planned", {"answers": {"same_intent": "yes"}}, "还没回答：['reasonable']"),
        (
            "planned",
            {"answers": {"same_intent": "maybe", "reasonable": "no"}},
            "不是问题 'same_intent' 的选项",
        ),
        (
            "planned",
            {"answers": {"same_intent": "yes", "reasonable": "no", "executed_as_intended": "yes"}},
            "这个条目没有问问题 'executed_as_intended'",
        ),
        (
            "only-reasonable",
            {"choice": "yes", "answers": {"reasonable": "yes"}},
            "请提交 'answers'",
        ),
        ("planned", {"skipped": True, "answers": {"reasonable": "no"}}, "不带答案"),
    ],
)
def test_question_labels_are_validated(store, item, draft, reason):
    with pytest.raises(AnnotationError, match=re.escape(reason)):
        label(store, "fork-questions", item, **draft)


def test_answers_are_refused_by_a_single_question_queue(store):
    with pytest.raises(AnnotationError, match="只有一个问题"):
        label(store, "fork-judge", "run/s0", choice="reasonable", answers={"x": "y"})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda data: data["label"].update(choices=[{"value": "x"}]), "不能两者都有"),
        (lambda data: data["label"].update(questions=[]), "至少要有一个问题"),
        (
            lambda data: data["label"]["questions"].append(data["label"]["questions"][0]),
            "问题的 key不能重复",
        ),
        (lambda data: data["items"][2].update(questions=["unknown"]), "问了队列里没有的问题"),
        (lambda data: data["items"][2].update(questions=[]), "没有问队列里的任何问题"),
        (
            lambda data: data["items"][0]["suggestion"]["answers"].update(
                executed_as_intended="yes"
            ),
            "这个条目没有问问题",
        ),
        (
            lambda data: data["items"][0]["suggestion"].update(
                reasons={"executed_as_intended": "x"}
            ),
            "但这个条目没有问它",
        ),
    ],
)
def test_question_queues_are_checked_when_loaded(change, reason):
    data = questions_queue("run").model_dump(mode="json")
    change(data)
    with pytest.raises(ValueError, match=re.escape(reason)):
        ReviewQueue.model_validate(data)


# ── review fixes: versions, torn lines, numbers, locking ──────


def test_a_label_for_a_rewritten_queue_is_refused(store: AnnotationStore):
    seen = store.load_queue("fork-judge").sha256
    write_queue(store.directory, fork_judge_queue("run", title="Rewritten"))
    draft = LabelDraft(choice="reasonable")
    with pytest.raises(QueueChangedError, match="在你打开之后被重新生成了"):
        store.append_label("fork-judge", "run/s0", draft, queue_sha256=seen, image_size=sizes)
    assert not (store.directory / "labels" / "fork-judge.jsonl").exists()
    now = store.load_queue("fork-judge").sha256
    line = store.append_label("fork-judge", "run/s0", draft, queue_sha256=now, image_size=sizes)
    assert line.queue_sha256 == now


def test_a_partly_written_last_line_is_skipped_without_an_error(store: AnnotationStore):
    label(store, "fork-judge", "run/s0", choice="reasonable")
    path = store.directory / "labels" / "fork-judge.jsonl"
    with path.open("a") as handle:
        handle.write('{"format": "review-label-1", "queue_id": "fork-ju')
    log = store.labels("fork-judge")
    assert [line.item_id for line in log.lines] == ["run/s0"] and not log.errors


def test_a_suggestion_with_no_answers_breaks_the_queue(tmp_path: Path):
    data = questions_queue("run").model_dump(mode="json")
    data["items"][0]["suggestion"]["answers"] = {}
    data["items"][0]["suggestion"]["reasons"] = None
    (tmp_path / "queues").mkdir()
    (tmp_path / "queues" / "fork-questions.json").write_text(json.dumps(data))
    [listing] = AnnotationStore(tmp_path, annotator="a").list_queues()
    assert listing.loaded is None and "的建议没有任何答案" in (listing.error or "")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_box_numbers_must_be_finite(value):
    with pytest.raises(ValueError):
        Region(image="before", kind="evidence", box=[0, 0, value, 5])
    with pytest.raises(ValueError):
        token = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}[str(value)]
        Region.model_validate_json(
            f'{{"image": "before", "kind": "evidence", "box": [0, 0, {token}, 5]}}'
        )


def test_queue_files_with_non_finite_numbers_are_broken(tmp_path: Path):
    data = evidence_queue("run", "frame.png").model_dump(mode="json")
    text = json.dumps(data).replace("[1, 2, 30, 20]", "[1, 2, NaN, 20]")
    (tmp_path / "queues").mkdir()
    (tmp_path / "queues" / "gold-evidence.json").write_text(text)
    [listing] = AnnotationStore(tmp_path, annotator="a").list_queues()
    assert listing.loaded is None


@pytest.mark.skipif(sys.platform == "win32", reason="flock is the POSIX lock")
def test_an_append_waits_for_another_holder_of_the_lock(store: AnnotationStore):
    import fcntl

    path = store.directory / "labels" / "fork-judge.jsonl"
    path.parent.mkdir()
    path.touch()
    finished = threading.Event()

    def append() -> None:
        label(store, "fork-judge", "run/s0", choice="reasonable")
        finished.set()

    with path.open("rb") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        writer = threading.Thread(target=append)
        writer.start()
        # Without the lock the append would be done long before this.
        assert not finished.wait(0.5)
        assert path.read_bytes() == b""
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    writer.join(5)
    assert finished.is_set() and len(store.labels("fork-judge").lines) == 1


def test_the_store_imports_no_posix_only_module_at_load():
    import spectragent.annotation.store as module

    assert "fcntl" not in vars(module)


# ── what a person reads: help, box rules, guide, technical fields, marks ──


def _ruled_queue(**choice_rules: str) -> dict:
    data = evidence_queue("run", "frame.png").model_dump(mode="json")
    data["label"]["multi"] = False
    data["label"]["choices"] = [
        {"value": value, "text": value, "help": f"{value} 的说明", "regions": rule}
        for value, rule in choice_rules.items()
    ]
    for item in data["items"]:
        item["suggestion"] = None
    return data


def test_a_box_rule_needs_a_queue_that_labels_regions() -> None:
    data = fork_judge_queue("run").model_dump(mode="json")
    data["label"]["choices"][0]["regions"] = "required"
    with pytest.raises(ValueError, match="这个队列不画框"):
        ReviewQueue.model_validate(data)


def test_a_suggestion_must_keep_the_box_rule_of_its_choice() -> None:
    data = _ruled_queue(nothing_new="forbidden", read="required")
    box = {"image": "before", "kind": "evidence", "box": [0, 0, 4, 4]}
    data["items"][0]["suggestion"] = {"choice": "nothing_new", "regions": [box]}
    with pytest.raises(ValueError, match="就不能画框"):
        ReviewQueue.model_validate(data)
    # a suggestion that proposes no boxes at all is not held to a rule about boxes
    data["items"][0]["suggestion"] = {"choice": "read"}
    ReviewQueue.model_validate(data)


def test_picking_choices_with_opposite_box_rules_is_refused(tmp_path: Path) -> None:
    data = _ruled_queue(nothing_new="forbidden", read="required")
    data["label"]["multi"] = True
    write_queue(tmp_path, ReviewQueue.model_validate(data))
    store = AnnotationStore(tmp_path, annotator="ana")
    with pytest.raises(AnnotationError, match="不能同时选"):
        label(store, "gold-evidence", "first", choices=["nothing_new", "read"])


def test_queue_reading_aids_round_trip() -> None:
    data = _ruled_queue(other="optional")
    data["guide"] = [{"title": "例子", "text": "- 一个例子"}]
    data["suggestion_display"] = {"title": "模型的判断（仅供参考）", "shown": "on_request"}
    item = data["items"][0]
    item["fields"] = [{"text": "原始参数", "value": "{}", "technical": True}]
    item["images"][0]["marks_from"] = [{"run": "other", "step": 2, "label": "原运行的动作"}]
    queue = ReviewQueue.model_validate(data)
    assert queue.guide[0].title == "例子" and queue.suggestion_display.shown == "on_request"
    assert queue.items[0].fields[0].technical
    assert queue.items[0].images[0].marks_from is not None
    assert queue.label.choices[0].help == "other 的说明"
    assert ReviewQueue.model_validate(json.loads(queue.model_dump_json())) == queue
    data["suggestion_display"]["shown"] = "later"
    with pytest.raises(ValueError):
        ReviewQueue.model_validate(data)
