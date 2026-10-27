"""``judge-forks``: rules and the judge model on each branch's decision at its checkpoint."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lookahead.cli import run
from lookahead.forks import (
    SOURCE_ONLY,
    Answer,
    EarlierAnswers,
    IntentJudgment,
    QualityJudgment,
    ReasonablenessJudgment,
    compare_urls,
    fork_rule,
    forks,
    judge_forks,
    judgment_record,
    markers,
    quality_questions,
    quality_request,
    queue_item,
    render_report,
    rule_verdict,
    volatile_parameter,
)
from lookahead.judge import Judge, JudgeOutcome
from lookahead.rows import ActionRow, NextObservation
from lookahead.runs import open_runs
from spectragent.core.llm.exceptions import ModelProviderError
from spectragent.core.llm.messages import BaseMessage
from tests.test_lookahead.conftest import TASK, VARIANT, VARIANT_TASK, World
from tests.test_lookahead.fake_judge import (
    ScriptedChatModel,
    answers,
    images,
    judge_model,
    loader,
    prompt_text,
    refuse_loader,
)


def _action(
    name: str,
    *,
    hit: Any = None,
    text: str | None = None,
    at: tuple[int, int] | None = None,
    target: Any = None,
    **params: Any,
) -> ActionRow:
    facts: dict[str, Any] = {}
    if hit is not None:
        facts["hit"] = hit
    if target is not None:
        facts["target"] = target
    if text is not None:
        facts["text"] = text
        params["text"] = text
    if at is not None:
        params["coordinate_x"], params["coordinate_y"] = at
    return ActionRow(index=0, name=name, params=params, facts=facts)


NO_NEXT = NextObservation(step=None, exists=False)


def _next(url: str) -> NextObservation:
    return NextObservation(step=2, exists=True, url=url)


BUTTON = {"role": "button", "name": "Search"}
FIELD = {"role": "textbox", "name": "Search"}
SLIDER = {"role": "slider", "name": "Price"}
CLICK = "browser.click_coordinate"


@pytest.mark.parametrize(
    ("branch", "source", "expected"),
    [
        # pointer: the same named element at nearby points
        (
            [_action(CLICK, hit=BUTTON, at=(100, 10))],
            [_action(CLICK, hit=BUTTON, at=(110, 12))],
            "same",
        ),
        # the same name far apart: possibly two items of that name
        (
            [_action(CLICK, hit=BUTTON, at=(100, 10))],
            [_action(CLICK, hit=BUTTON, at=(600, 400))],
            "unknown",
        ),
        # two named interactive targets
        (
            [_action(CLICK, hit=BUTTON, at=(100, 10))],
            [_action(CLICK, hit={"role": "link", "name": "About"}, at=(300, 10))],
            "different",
        ),
        # a card's image and its title link: possibly the same item
        (
            [_action(CLICK, hit={"role": "img", "name": ""}, at=(100, 100))],
            [_action(CLICK, hit={"role": "link", "name": "Headphones X"}, at=(100, 180))],
            "unknown",
        ),
        (
            [_action(CLICK, hit={"role": "img", "name": ""})],
            [_action(CLICK, hit={"role": "img", "name": ""})],
            "unknown",
        ),
        ([_action(CLICK, hit="unknown")], [_action("browser.click", hit=BUTTON)], "unknown"),
        # a drag aims where it starts: its first declared point
        (
            [_action("browser.drag", hit=SLIDER, start_x=10, start_y=10, end_x=90, end_y=10)],
            [_action("browser.drag", hit=SLIDER, start_x=12, start_y=11, end_x=40, end_y=10)],
            "same",
        ),
        (
            [_action("browser.drag", hit=SLIDER, start_x=10, start_y=10, end_x=90, end_y=10)],
            [_action("browser.drag", hit=SLIDER, start_x=300, start_y=200, end_x=90, end_y=10)],
            "unknown",
        ),
        # typing: the same text into the same field
        (
            [_action("browser.type", text="耳机 ", target=FIELD)],
            [_action("browser.type_coordinate", text="耳机", target=FIELD, at=(50, 10))],
            "same",
        ),
        (
            [_action("browser.type", text="耳机", target=FIELD)],
            [_action("browser.type", text="耳机", target={"role": "textbox", "name": "Coupon"})],
            "different",
        ),
        (
            [_action("browser.type_coordinate", text="耳机", at=(50, 10))],
            [_action("browser.type_coordinate", text="耳机", at=(55, 12))],
            "same",
        ),
        ([_action("browser.type", text="耳机")], [_action("browser.type", text="耳机")], "unknown"),
        (
            [_action("browser.type", text="耳机")],
            [_action("browser.type", text="手机")],
            "different",
        ),
        (
            [_action("browser.scroll", direction="down")],
            [_action("browser.scroll", direction="up")],
            "different",
        ),
        ([_action("browser.wait")], [_action("browser.wait")], "same"),
        ([_action("browser.wait")], [_action("browser.click", hit=BUTTON)], "different"),
        (
            [_action("browser.done", extracted_content="¥99")],
            [_action("browser.done", extracted_content="99 yuan")],
            "unknown",
        ),
        (
            [_action(CLICK, hit=BUTTON, at=(1, 1)), _action("browser.type", text="x")],
            [_action(CLICK, hit=BUTTON, at=(1, 1))],
            "unknown",
        ),
        (
            [_action("browser.click", hit=BUTTON)],
            [_action("browser.scroll", direction="down")],
            "different",
        ),
    ],
)
def test_rules(branch: list[ActionRow], source: list[ActionRow], expected: str) -> None:
    assert rule_verdict(branch, source, NO_NEXT, NO_NEXT).verdict == expected


def test_rings_mark_pointer_actions_on_the_checkpoint_image(world: World) -> None:
    runs = {run.key: run for run in open_runs(world.root).runs}
    branch, source = runs["branches/br-same"], runs["record/rec-1"]
    found = markers(branch, 1, branch, 1, "B", "#e8202a")
    assert [(ring.label, ring.x, ring.y) for ring in found] == [("B1", 101, 11)]
    # the source's step 0 clicked and waited: only the click points anywhere
    found = markers(branch, 1, source, 0, "A", "#1f6feb")
    assert [(ring.label, ring.x, ring.y) for ring in found] == [("A1", 20, 10)]
    # typing by element index names no point
    assert markers(runs["branches/br-typed"], 1, runs["branches/br-typed"], 1, "B", "red") == []


def test_an_error_without_an_action_is_no_decision() -> None:
    clicked = [_action(CLICK, hit=BUTTON, at=(1, 1))]
    parse = "parse: could not parse the reply"
    assert rule_verdict([], clicked, NO_NEXT, NO_NEXT, branch_error=parse).verdict == "no_decision"
    assert rule_verdict(clicked, [], NO_NEXT, NO_NEXT, source_error=parse).verdict == "no_decision"
    # an error after acting is still a decision
    assert (
        rule_verdict(clicked, clicked, NO_NEXT, NO_NEXT, branch_error="act: timeout").verdict
        == "same"
    )
    # no action without an error is a decision too: to do nothing
    assert rule_verdict([], clicked, NO_NEXT, NO_NEXT).verdict == "different"


def test_the_next_url_decides_only_when_the_page_differs() -> None:
    same = [_action(CLICK, hit=BUTTON, at=(1, 1))]

    def verdict(left: str, right: str) -> str:
        return rule_verdict(same, same, _next(left), _next(right)).verdict

    assert verdict("https://a/x", "https://a/y") == "different"
    assert verdict("https://a/x?vt=12345678&q=1", "https://a/x?q=1&vt=87654321") == "same"
    assert verdict("https://a/x?q=1&utm_source=x", "https://a/x?q=1") == "same"
    assert verdict("https://a/x?order=click", "https://a/x?order=pubdate") == "unknown"
    assert verdict("https://a/x", "https://a/x") == "same"
    assert compare_urls(None, "https://a/x")[0] is None
    assert volatile_parameter("w_rid", "x") and volatile_parameter("id", "1761000000123")
    assert not volatile_parameter("keyword", "耳机")


def _forks(world: World) -> dict[str, Any]:
    return {fork.branch.key: fork for fork in forks(open_runs(world.root))}


def test_branches_are_paired_and_classified_by_their_recorded_facts(world: World) -> None:
    found = _forks(world)
    same = found["branches/br-same"]
    assert same.counted and same.source is not None and same.source.key == "record/rec-1"
    assert same.branch_row.step == 1 and same.source_row.step == 1
    assert same.checkpoint_check.verdict == "ok"
    assert found["branches/br-variant"].kind == "task_variant"
    assert found["branches/br-variant"].variant == VARIANT
    ended = found["branches/br-ended"]
    assert ended.excluded == "checkpoint_diverged"
    assert "restoration stopped at step 0" in ended.reason
    offpage = found["branches/br-offpage"]
    assert offpage.excluded == "checkpoint_diverged"
    assert "chrome-error://chromewebdata/" in offpage.reason and "url_mismatch" in offpage.reason
    unparsed = found["branches/br-unparsed"]
    assert unparsed.excluded == "no_decision" and "parse" in unparsed.reason


def _intent_request(messages: list[BaseMessage]) -> bool:
    return "Two runs of an agent" in prompt_text(messages)


def _script(messages: list[BaseMessage]) -> str:
    """Same intent unless run B typed; the branch alone is reasonable but not surely executed."""
    text = prompt_text(messages)
    if _intent_request(messages):
        branch_action = next(line for line in text.splitlines() if line.startswith("B1. "))
        return answers(same_intent="no" if "browser.type" in branch_action else "yes")
    return answers(reasonable="yes", executed_as_intended="uncertain")


def _judge_forks(world: World, tmp_path: Path, llm: ScriptedChatModel):
    judge = Judge(judge_model(llm))
    return asyncio.run(
        judge_forks(
            open_runs(world.root),
            judge,
            tmp_path / "forks",
            annotations=tmp_path / "ann",
            queue_id="fork-judge-test",
        )
    )


def test_judgments_keep_rules_model_answers_and_the_checkpoint_check(
    world: World, tmp_path: Path
) -> None:
    llm = ScriptedChatModel(_script)
    records, queue_path = _judge_forks(world, tmp_path, llm)
    by_run = {record["branch_run"]: record for record in records}
    same = by_run["branches/br-same"]
    assert same["status"] == "judged" and same["rule"]["verdict"] == "same"
    assert same["llm"]["same_intent"]["answer"] == "yes"
    assert same["llm"]["reasonable"]["answer"] == "yes"
    assert same["checkpoint_check"]["verdict"] == "ok"
    typed = by_run["branches/br-typed"]
    assert typed["rule"]["verdict"] == "different"
    assert typed["llm"]["same_intent"]["answer"] == "no"
    variant = by_run["branches/br-variant"]
    assert variant["fork_kind"] == "task_variant" and variant["task"] == VARIANT_TASK
    assert "same_intent" not in variant["llm"]
    assert variant["llm"]["executed_as_intended"]["answer"] == "uncertain"
    offpage = by_run["branches/br-offpage"]
    assert offpage["status"] == "checkpoint_diverged" and offpage["llm"] is None
    assert offpage["checkpoint_check"]["restored_url"] == "chrome-error://chromewebdata/"
    assert offpage["llm_calls"] == [] and offpage["rule"] is None
    assert by_run["branches/br-unparsed"]["status"] == "no_decision"
    assert by_run["branches/br-ended"]["status"] == "checkpoint_diverged"
    # two separate requests per same-task fork, one per variant fork
    assert len(llm.calls) == 5
    assert queue_path is not None


def test_quality_requests_carry_no_source_information(world: World, tmp_path: Path) -> None:
    llm = ScriptedChatModel(_script)
    _judge_forks(world, tmp_path, llm)
    asked = list(zip(llm.calls, llm.formats, strict=True))
    intent = [messages for messages, reply in asked if _intent_request(messages)]
    quality = [messages for messages, reply in asked if not _intent_request(messages)]
    assert len(intent) == 2 and len(quality) == 3
    assert {reply for messages, reply in asked if _intent_request(messages)} == {IntentJudgment}
    assert {reply for messages, reply in asked if not _intent_request(messages)} == {
        QualityJudgment
    }
    for messages in quality:
        text = prompt_text(messages)
        assert "Thought: run the search." not in text  # the source's reply
        assert "S1." not in text and "Run A" not in text and "original" not in text
        assert len(images(messages)) == 2  # the checkpoint and the branch's next screen
    variant = next(messages for messages in quality if VARIANT_TASK in prompt_text(messages))
    assert TASK not in prompt_text(variant)
    assert 'measured: Clicked at (10, 90) on link "Returns"' in prompt_text(variant)
    same = intent[0]
    assert "Run A's actions" in prompt_text(same) and "Run B's actions" in prompt_text(same)
    assert len(images(same)) == 3  # the checkpoint and both next screens


def test_report_counts_only_judged_forks_and_lists_the_others(world: World, tmp_path: Path) -> None:
    _judge_forks(world, tmp_path, ScriptedChatModel(_script))
    report = (tmp_path / "forks" / "report.md").read_text(encoding="utf-8")
    assert "Judged (paired, restored at the checkpoint, decided by the branch): 3" in report
    assert "The rules decide (same or different) 2/2 (100%) of the same-task forks" in report
    assert "they agree on 2/2 (100%)" in report
    assert "| different | 0 | 1 | 0 | 0 | 0 | 1 |" in report
    assert "The checkpoint did not restore: 2." in report
    assert "chrome-error://chromewebdata/" in report
    assert "The branch made no decision: 1." in report
    lines = (tmp_path / "forks" / "judgments.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6


def test_queue_shows_source_information_only_for_the_intent_question(
    world: World, tmp_path: Path
) -> None:
    _, path = _judge_forks(world, tmp_path, ScriptedChatModel(_script))
    assert path is not None
    queue = json.loads(path.read_text(encoding="utf-8"))
    label = queue["label"]
    assert label["regions"] is None and label["choices"] == [] and label["multi"] is False
    assert [question["key"] for question in label["questions"]] == [
        "reasonable",
        "executed_as_intended",
        "same_intent",
    ]
    for question in label["questions"]:
        assert [choice["value"] for choice in question["choices"]] == ["yes", "no", "uncertain"]
    items = {item["item_id"]: item for item in queue["items"]}
    assert set(items) == {"branches/br-same@1", "branches/br-typed@1", "branches/br-variant@1"}
    same = items["branches/br-same@1"]
    assert same["questions"] == ["reasonable", "executed_as_intended", "same_intent"]
    assert same["suggestion"]["answers"] == {
        "same_intent": "yes",
        "reasonable": "yes",
        "executed_as_intended": "uncertain",
    }
    assert same["suggestion"]["reasons"]["same_intent"] == "same_intent is yes."
    assert same["suggestion"]["source"] == "fake-judge"
    assert queue["suggestion_display"] == {"title": "模型的判断（仅供参考）", "shown": "on_request"}
    assert queue["guide"] and all(question["help"] for question in label["questions"])
    shown = [
        field for field in same["fields"] if "原运行" in field["text"] and not field["technical"]
    ]
    assert shown and all(field["text"].endswith(SOURCE_ONLY) for field in shown)
    assert shown[0]["value"] == "1. 点击 (100, 10) → 按钮“Search”"
    rule = next(field for field in same["fields"] if field["text"].startswith("规则判断"))
    assert rule["technical"] and rule["value"].startswith("same: ")
    images = {image["role"]: image for image in same["images"]}
    assert images["source_next"]["text"].endswith(SOURCE_ONLY)
    assert images["source_next"]["source"] == {
        "kind": "observation",
        "run": "record/rec-1",
        "step": 2,
    }
    assert same["trajectory"] is True
    assert same["sections"][0]["key"] == "source_comparison"
    assert images["source_checkpoint"]["section"] == "source_comparison"
    assert images["source_next"]["section"] == "execution_comparison"
    assert images["comparison_checkpoint"]["section"] == "source_comparison"
    assert images["comparison_branch_next"]["section"] == "execution_comparison"
    assert all(field["section"] == "source_comparison" for field in shown)
    # Main evidence contains only this branch; source evidence is independently grouped.
    assert [(entry["run"], entry["step"]) for entry in images["checkpoint"]["marks_from"]] == [
        ("branches/br-same", 1),
    ]
    assert images["source_checkpoint"]["marks_from"] == [
        {"run": "record/rec-1", "step": 1, "label": "原运行的动作"}
    ]
    assert images["branch_next"]["marks_from"] == [] and images["source_next"]["marks_from"] == []
    variant = items["branches/br-variant@1"]
    assert variant["questions"] == ["reasonable", "executed_as_intended"]
    assert set(variant["suggestion"]["answers"]) == {"reasonable", "executed_as_intended"}
    assert not [
        field for field in variant["fields"] if "原运行" in field["text"] or "规则" in field["text"]
    ]
    assert [image["role"] for image in variant["images"]] == ["checkpoint", "branch_next"]
    assert [entry["run"] for entry in variant["images"][0]["marks_from"]] == ["branches/br-variant"]
    assert [link["run"] for link in variant["links"]] == ["branches/br-variant"]
    assert variant["title"] == "shop.example · 任务 rec-1 · 分叉 · 检查点第 1 步 · 换任务"


def test_a_fork_without_a_valid_reply_keeps_its_rule_verdict(world: World, tmp_path: Path) -> None:
    records, path = _judge_forks(world, tmp_path, ScriptedChatModel(lambda messages: "{}"))
    same = next(record for record in records if record["branch_run"] == "branches/br-same")
    assert same["status"] == "rules_only" and same["rule"]["verdict"] == "same"
    assert same["llm"] is None and same["llm_errors"]["quality"] is not None
    assert len(same["llm_calls"]) == 6  # both requests, three attempts each
    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8"))["items"] == []


def test_cli_rules_only_asks_no_model(world: World, tmp_path: Path) -> None:
    out = tmp_path / "forks"
    code = run(
        ["judge-forks", "--root", str(world.root), "--out", str(out), "--rules-only"],
        load_judge=refuse_loader,
    )
    assert code == 0
    assert "No judge model was asked." in (out / "report.md").read_text(encoding="utf-8")
    assert not (out / "queues").exists()


def test_cli_reuses_earlier_answers_without_asking_a_model(world: World, tmp_path: Path) -> None:
    first = tmp_path / "first"
    code = run(
        [
            "judge-forks",
            "--root",
            str(world.root),
            "--out",
            str(first),
            "--queue-id",
            "f",
            "--judge-config",
            "judge.yaml",
        ],
        load_judge=loader(ScriptedChatModel(_script)),
    )
    assert code == 0
    again = tmp_path / "again"
    code = run(
        [
            "judge-forks",
            "--root",
            str(world.root),
            "--out",
            str(again),
            "--queue-id",
            "f",
            "--answers-from",
            str(first / "judgments.jsonl"),
        ],
        load_judge=refuse_loader,
    )
    assert code == 0
    earlier = json.loads((first / "queues" / "f.json").read_text(encoding="utf-8"))
    reused = json.loads((again / "queues" / "f.json").read_text(encoding="utf-8"))
    assert [item["suggestion"] for item in reused["items"]] == [
        item["suggestion"] for item in earlier["items"]
    ]
    records = [
        json.loads(line)
        for line in (again / "judgments.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    judged = [record for record in records if record["status"] == "judged"]
    assert len(judged) == 3 and all(record["llm_calls"] == [] for record in judged)
    assert all(record["answers_from"] == str(first / "judgments.jsonl") for record in judged)
    assert "reused from" in (again / "report.md").read_text(encoding="utf-8")


def test_answers_from_refuses_a_file_of_another_format(world: World, tmp_path: Path) -> None:
    other = tmp_path / "other.jsonl"
    other.write_text('{"format": "something-else"}\n', encoding="utf-8")
    code = run(
        [
            "judge-forks",
            "--root",
            str(world.root),
            "--out",
            str(tmp_path / "out"),
            "--answers-from",
            str(other),
        ]
    )
    assert code == 2


def test_cli_with_a_judge_writes_calls_and_queue(world: World, tmp_path: Path) -> None:
    llm = ScriptedChatModel(_script)
    out = tmp_path / "forks"
    composed: list[Path] = []

    def load(path: Path):
        composed.append(path)
        return judge_model(llm, path)

    code = run(
        [
            "judge-forks",
            "--root",
            str(world.root),
            "--out",
            str(out),
            "--queue-id",
            "forks-x",
            "--judge-config",
            str(tmp_path / "judge.yaml"),
        ],
        load_judge=load,
    )
    assert code == 0 and llm.closed == 1 and composed == [tmp_path / "judge.yaml"]
    assert (out / "queues" / "forks-x.json").is_file()
    assert len((out / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 5


def test_cli_stops_when_the_route_refuses_the_probe(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    llm = ScriptedChatModel(
        lambda messages: ModelProviderError("response_format not supported", status_code=400)
    )
    out = tmp_path / "forks"
    code = run(
        ["judge-forks", "--root", str(world.root), "--out", str(out), "--judge-config", "j.yaml"],
        load_judge=loader(llm),
    )
    assert code == 2 and len(llm.calls) == 1 and llm.closed == 1
    assert "response_format not supported" in capsys.readouterr().err
    assert not (out / "judgments.jsonl").exists() and not (out / "report.md").exists()


def test_cli_needs_a_judge_configuration_or_no_model(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        run(["judge-forks", "--root", str(world.root), "--out", str(tmp_path / "out")])
    assert exited.value.code == 2
    assert "--judge-config" in capsys.readouterr().err


def test_cli_refuses_a_judge_configuration_it_cannot_read(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "forks"
    code = run(
        [
            "judge-forks",
            "--root",
            str(world.root),
            "--out",
            str(out),
            "--judge-config",
            str(tmp_path / "absent.yaml"),
        ]
    )
    assert code == 2 and "absent.yaml cannot be read" in capsys.readouterr().err
    assert not out.exists()


def test_done_only_has_no_execution_effect_question(world: World) -> None:
    fork = next(
        fork for fork in forks(open_runs(world.root)) if fork.branch.key == "branches/br-same"
    )
    assert fork.branch_row is not None
    fork.branch_row = fork.branch_row.model_copy(
        update={"actions": [_action("browser.done", extracted_content="Answer")]}
    )
    assert quality_questions(fork.branch_row) == ("reasonable",)
    request = quality_request(fork)
    assert request is not None and request.reply_model is ReasonablenessJudgment
    item = queue_item(
        {
            "llm": {
                "reasonable": {"answer": "yes", "reason": "Answer is supported."},
                "executed_as_intended": {"answer": "no", "reason": "Old answer."},
                "same_intent": {"answer": "yes", "reason": "Same answer."},
            },
            "task": fork.branch.config.task,
            "rule": {},
            "model": "judge",
        },
        fork,
    )
    assert item is not None and item.questions == ["reasonable", "same_intent"]
    assert item.suggestion is not None
    assert set(item.suggestion.answers or {}) == {"reasonable", "same_intent"}
    mixed = fork.branch_row.model_copy(
        update={"actions": [_action("browser.click"), _action("browser.done")]}
    )
    assert quality_questions(mixed) == ("reasonable", "executed_as_intended")


@pytest.mark.parametrize("reuse", [False, True])
def test_done_only_applicability_matches_record_report_and_queue(
    world: World, tmp_path: Path, reuse: bool
) -> None:
    fork = next(
        fork for fork in forks(open_runs(world.root)) if fork.branch.key == "branches/br-same"
    )
    assert fork.branch_row is not None
    fork.branch_row = fork.branch_row.model_copy(
        update={"actions": [_action("browser.done", extracted_content="Answer")]}
    )
    answer = {"answer": "yes", "reason": "The answer is supported."}
    previous = (
        EarlierAnswers(
            tmp_path / "old-judgments.jsonl",
            {
                (fork.branch.run_id, fork.checkpoint): (
                    {"reasonable": answer, "same_intent": answer, "executed_as_intended": answer},
                    "old-judge",
                )
            },
        )
        if reuse
        else None
    )
    parsed_answer = Answer.model_validate(answer)
    intent = None if reuse else JudgeOutcome(IntentJudgment(same_intent=parsed_answer), [])
    quality = None if reuse else JudgeOutcome(ReasonablenessJudgment(reasonable=parsed_answer), [])
    record = judgment_record(fork, fork_rule(fork), intent, quality, "new-judge", previous)
    assert record["questions"] == ["reasonable", "same_intent"]
    assert set(record["llm"]) == {"reasonable", "same_intent"}
    assert record["llm"]["reasonable"] == answer
    assert record["answers_from"] == (str(previous.path) if previous else None)
    report = render_report([record], "No live calls")
    quality_table = report.split("same-task forks, reasonable × executed_as_intended:", 1)[1].split(
        "## Rule verdicts", 1
    )[0]
    assert "not applicable" in quality_table
    assert "| yes | 0 | 0 | 0 | 0 | 1 | 1 |" in quality_table
    item = queue_item(record, fork)
    assert item is not None and item.suggestion is not None
    assert item.questions == record["questions"]
    assert set(item.suggestion.answers or {}) == set(record["questions"])
