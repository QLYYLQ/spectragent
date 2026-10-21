"""``judge-forks``: rules and the judge model on each branch's decision at its checkpoint."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lookahead.cli import run
from lookahead.forks import compare_urls, forks, judge_forks, rule_verdict
from lookahead.judge import Judge
from lookahead.rows import ActionRow, NextObservation
from lookahead.runs import open_runs
from tests.test_lookahead.conftest import TASK, VARIANT_TASK, World
from tests.test_lookahead.fake_judge import ScriptedTransport, answers, images, prompt_text
from tests.test_lookahead.traces import ContractTrace


def _action(name: str, *, hit: Any = None, text: str | None = None, **params: Any) -> ActionRow:
    facts: dict[str, Any] = {}
    if hit is not None:
        facts["hit"] = hit
    if text is not None:
        facts["text"] = text
        params["text"] = text
    return ActionRow(index=0, name=name, params=params, facts=facts)


NO_NEXT = NextObservation(step=None, exists=False)


def _next(url: str) -> NextObservation:
    return NextObservation(step=2, exists=True, url=url)


BUTTON = {"role": "button", "name": "Search"}


@pytest.mark.parametrize(
    ("branch", "source", "expected"),
    [
        (
            [_action("browser.click_coordinate", hit=BUTTON)],
            [_action("browser.click", hit=BUTTON)],
            "same",
        ),
        (
            [_action("browser.click_coordinate", hit=BUTTON)],
            [_action("browser.click_coordinate", hit={"role": "link", "name": "About"})],
            "different",
        ),
        (
            [_action("browser.click_coordinate", hit={"role": "img", "name": ""})],
            [_action("browser.click_coordinate", hit={"role": "img", "name": ""})],
            "unknown",
        ),
        (
            [_action("browser.click_coordinate", hit="unknown")],
            [_action("browser.click", hit=BUTTON)],
            "unknown",
        ),
        (
            [_action("browser.type", text="耳机 ")],
            [_action("browser.type_coordinate", text="耳机")],
            "same",
        ),
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
            [_action("browser.click", hit=BUTTON), _action("browser.type", text="x")],
            [_action("browser.click", hit=BUTTON)],
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


def test_the_next_url_decides_only_when_the_page_differs() -> None:
    same = [_action("browser.click", hit=BUTTON)]
    assert (
        rule_verdict(same, same, _next("https://a/x"), _next("https://a/y")).verdict == "different"
    )
    assert (
        rule_verdict(same, same, _next("https://a/x?v=1"), _next("https://a/x?v=2")).verdict
        == "unknown"
    )
    assert rule_verdict(same, same, _next("https://a/x"), _next("https://a/x")).verdict == "same"
    assert compare_urls(None, "https://a/x")[0] is None


def _forks(world: World) -> dict[str, Any]:
    return {fork.branch.key: fork for fork in forks(open_runs(world.root, opener=ContractTrace))}


def test_branches_are_paired_with_the_source_step_through_their_provenance(world: World) -> None:
    found = _forks(world)
    same = found["branches/br-same"]
    assert same.source is not None and same.source.key == "record/rec-1"
    assert same.branch_row.step == 1 and same.source_row.step == 1
    assert same.kind == "same_task"
    assert found["branches/br-variant"].kind == "task_variant"
    assert found["branches/br-variant"].variant["task_text"] == VARIANT_TASK
    ended = found["branches/br-ended"]
    assert ended.unpaired is not None and "no policy step at its checkpoint" in ended.unpaired


def _script(request: dict[str, Any]) -> str:
    """Same intent unless the branch typed where the source clicked."""
    text = prompt_text(request)
    if "same_intent" in text:
        branch_action = next(line for line in text.splitlines() if line.startswith("B1. "))
        intent = "no" if "browser.type" in branch_action else "yes"
        return answers(same_intent=intent, reasonable="yes", executed_as_intended="yes")
    return answers(reasonable="yes", executed_as_intended="uncertain")


def _judge_forks(world: World, tmp_path: Path, transport: ScriptedTransport):
    judge = Judge(transport)
    runs = open_runs(world.root, opener=ContractTrace)
    return asyncio.run(
        judge_forks(
            runs,
            judge,
            tmp_path / "forks",
            annotations=tmp_path / "ann",
            queue_id="fork-judge-test",
        )
    )


def test_judgments_rules_and_model_answers(world: World, tmp_path: Path) -> None:
    transport = ScriptedTransport(_script)
    records, queue_path = _judge_forks(world, tmp_path, transport)
    by_run = {record["branch_run"]: record for record in records}
    same = by_run["branches/br-same"]
    assert same["status"] == "judged" and same["rule"]["verdict"] == "same"
    assert same["llm"]["same_intent"]["answer"] == "yes"
    assert same["source_run"] == "record/rec-1" and same["checkpoint_step"] == 1
    typed = by_run["branches/br-typed"]
    assert typed["rule"]["verdict"] == "different"
    assert typed["llm"]["same_intent"]["answer"] == "no"
    variant = by_run["branches/br-variant"]
    assert variant["fork_kind"] == "task_variant" and variant["task"] == VARIANT_TASK
    assert variant["source_task"] == TASK
    assert variant["llm"]["same_intent"] is None
    assert variant["llm"]["executed_as_intended"]["answer"] == "uncertain"
    assert by_run["branches/br-ended"]["status"] == "unpaired"
    assert len(transport.requests) == 3
    assert queue_path is not None


def test_same_task_requests_show_both_sides_variant_requests_only_the_branch(
    world: World, tmp_path: Path
) -> None:
    transport = ScriptedTransport(_script)
    _judge_forks(world, tmp_path, transport)
    same_task = [request for request in transport.requests if "same_intent" in prompt_text(request)]
    variant = [
        request for request in transport.requests if "same_intent" not in prompt_text(request)
    ]
    assert len(same_task) == 2 and len(variant) == 1
    assert len(images(same_task[0])) == 3  # checkpoint, branch next, source next
    assert len(images(variant[0])) == 2  # checkpoint, branch next
    variant_text = prompt_text(variant[0])
    assert VARIANT_TASK in variant_text and TASK in variant_text
    assert "S1." not in variant_text  # the source's actions are not shown for a variant
    assert 'measured: Clicked at (10, 90) on link "Returns"' in variant_text


def test_report_answers_whether_rules_can_judge_forks(world: World, tmp_path: Path) -> None:
    _judge_forks(world, tmp_path, ScriptedTransport(_script))
    report = (tmp_path / "forks" / "report.md").read_text(encoding="utf-8")
    assert "The rules decide (same or different) 2/2 (100%) of the same-task forks" in report
    assert "they agree on 2/2 (100%)" in report
    assert "| different | 0 | 1 | 0 | 0 | 1 |" in report
    assert "## Not paired" in report and "branches/br-ended" in report
    lines = (tmp_path / "forks" / "judgments.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4


def test_queue_suggests_the_model_verdict(world: World, tmp_path: Path) -> None:
    _, path = _judge_forks(world, tmp_path, ScriptedTransport(_script))
    assert path is not None
    queue = json.loads(path.read_text(encoding="utf-8"))
    assert queue["label"]["multi"] is True and queue["label"]["regions"] is None
    items = {item["item_id"]: item for item in queue["items"]}
    assert set(items) == {"branches/br-same@1", "branches/br-typed@1", "branches/br-variant@1"}
    same = items["branches/br-same@1"]
    assert same["suggestion"]["choices"] == ["same_intent", "reasonable", "executed"]
    assert same["suggestion"]["source"] == "deepseek-v4.1-flash-1017"
    assert [image["role"] for image in same["images"]] == [
        "checkpoint",
        "branch_next",
        "source_next",
    ]
    assert same["images"][2]["source"] == {"kind": "observation", "run": "record/rec-1", "step": 2}
    variant = items["branches/br-variant@1"]
    assert variant["suggestion"]["choices"] == ["reasonable"]
    assert [link["run"] for link in variant["links"]] == ["branches/br-variant", "record/rec-1"]


def test_a_fork_without_a_valid_reply_keeps_its_rule_verdict(world: World, tmp_path: Path) -> None:
    records, path = _judge_forks(world, tmp_path, ScriptedTransport(lambda request: "{}"))
    same = next(record for record in records if record["branch_run"] == "branches/br-same")
    assert same["status"] == "rules_only" and same["rule"]["verdict"] == "same"
    assert same["llm"] is None and same["llm_error"] is not None
    assert len(same["llm_calls"]) == 3
    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8"))["items"] == []


def test_cli_rules_only_asks_no_model(world: World, tmp_path: Path) -> None:
    def refuse() -> ScriptedTransport:
        raise AssertionError("no model may be asked")

    out = tmp_path / "forks"
    code = run(
        ["judge-forks", "--root", str(world.root), "--out", str(out), "--rules-only"],
        transport_factory=refuse,
    )
    assert code == 0
    assert "No judge model was asked." in (out / "report.md").read_text(encoding="utf-8")
    assert not (out / "queues").exists()


def test_cli_with_a_judge_writes_calls_and_queue(world: World, tmp_path: Path) -> None:
    transport = ScriptedTransport(_script)
    out = tmp_path / "forks"
    code = run(
        ["judge-forks", "--root", str(world.root), "--out", str(out), "--queue-id", "forks-x"],
        transport_factory=lambda: transport,
    )
    assert code == 0 and transport.closed
    assert (out / "queues" / "forks-x.json").is_file()
    assert len((out / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 3
