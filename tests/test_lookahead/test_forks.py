"""``judge-forks``: rules and the judge model on each branch's decision at its checkpoint."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lookahead.cli import run
from lookahead.forks import (
    compare_urls,
    forks,
    judge_forks,
    markers,
    rule_verdict,
    volatile_parameter,
)
from lookahead.judge import Judge, TransportError
from lookahead.rows import ActionRow, NextObservation
from lookahead.runs import open_runs
from tests.test_lookahead.conftest import TASK, VARIANT, VARIANT_TASK, World
from tests.test_lookahead.fake_judge import ScriptedTransport, answers, images, prompt_text


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


def test_rings_mark_clicks_and_coordinate_typing(world: World) -> None:
    rows = {fork.branch.key: fork.branch_row for fork in forks(open_runs(world.root))}
    row = rows["branches/br-same"]
    assert row is not None
    typed = ActionRow(
        index=1,
        name="browser.type_coordinate",
        params={"coordinate_x": 40, "coordinate_y": 20, "text": "x"},
    )
    found = markers(row.model_copy(update={"actions": [*row.actions, typed]}), "A", "#e8202a")
    assert [(ring.label, ring.x, ring.y) for ring in found] == [("A1", 101, 11), ("A2", 40, 20)]


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


def _intent_request(request: dict[str, Any]) -> bool:
    return "Two runs of an agent" in prompt_text(request)


def _script(request: dict[str, Any]) -> str:
    """Same intent unless run B typed; the branch alone is reasonable but not surely executed."""
    text = prompt_text(request)
    if _intent_request(request):
        branch_action = next(line for line in text.splitlines() if line.startswith("B1. "))
        return answers(same_intent="no" if "browser.type" in branch_action else "yes")
    return answers(reasonable="yes", executed_as_intended="uncertain")


def _judge_forks(world: World, tmp_path: Path, transport: ScriptedTransport):
    judge = Judge(transport)
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
    transport = ScriptedTransport(_script)
    records, queue_path = _judge_forks(world, tmp_path, transport)
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
    assert variant["llm"]["same_intent"] is None
    assert variant["llm"]["executed_as_intended"]["answer"] == "uncertain"
    offpage = by_run["branches/br-offpage"]
    assert offpage["status"] == "checkpoint_diverged" and offpage["llm"] is None
    assert offpage["checkpoint_check"]["restored_url"] == "chrome-error://chromewebdata/"
    assert offpage["llm_calls"] == [] and offpage["rule"] is None
    assert by_run["branches/br-unparsed"]["status"] == "no_decision"
    assert by_run["branches/br-ended"]["status"] == "checkpoint_diverged"
    # two separate requests per same-task fork, one per variant fork
    assert len(transport.requests) == 5
    assert queue_path is not None


def test_quality_requests_carry_no_source_information(world: World, tmp_path: Path) -> None:
    transport = ScriptedTransport(_script)
    _judge_forks(world, tmp_path, transport)
    intent = [request for request in transport.requests if _intent_request(request)]
    quality = [request for request in transport.requests if not _intent_request(request)]
    assert len(intent) == 2 and len(quality) == 3
    for request in quality:
        text = prompt_text(request)
        assert "Thought: run the search." not in text  # the source's reply
        assert "S1." not in text and "Run A" not in text and "original" not in text
        assert len(images(request)) == 2  # the checkpoint and the branch's next screen
        assert set(
            json.loads(json.dumps(request["response_format"]))["json_schema"]["schema"][
                "properties"
            ]
        ) == {
            "reasonable",
            "executed_as_intended",
        }
    variant = next(request for request in quality if VARIANT_TASK in prompt_text(request))
    assert TASK not in prompt_text(variant)
    assert 'measured: Clicked at (10, 90) on link "Returns"' in prompt_text(variant)
    same = intent[0]
    assert "Run A's actions" in prompt_text(same) and "Run B's actions" in prompt_text(same)
    assert len(images(same)) == 3  # the checkpoint and both next screens


def test_report_counts_only_judged_forks_and_lists_the_others(world: World, tmp_path: Path) -> None:
    _judge_forks(world, tmp_path, ScriptedTransport(_script))
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
    _, path = _judge_forks(world, tmp_path, ScriptedTransport(_script))
    assert path is not None
    queue = json.loads(path.read_text(encoding="utf-8"))
    label = queue["label"]
    assert label["regions"] is None and label["choices"] == [] and label["multi"] is False
    assert [question["key"] for question in label["questions"]] == [
        "same_intent",
        "reasonable",
        "executed_as_intended",
    ]
    for question in label["questions"]:
        assert [choice["value"] for choice in question["choices"]] == ["yes", "no", "uncertain"]
    items = {item["item_id"]: item for item in queue["items"]}
    assert set(items) == {"branches/br-same@1", "branches/br-typed@1", "branches/br-variant@1"}
    same = items["branches/br-same@1"]
    assert same["questions"] == ["same_intent", "reasonable", "executed_as_intended"]
    assert same["suggestion"]["answers"] == {
        "same_intent": "yes",
        "reasonable": "yes",
        "executed_as_intended": "uncertain",
    }
    assert same["suggestion"]["reasons"]["same_intent"] == "same_intent is yes."
    assert same["suggestion"]["source"] == "deepseek-v4.1-flash-1017"
    source_fields = [field["text"] for field in same["fields"] if "original" in field["text"]]
    assert source_fields and all(text.startswith("For same intent: ") for text in source_fields)
    source_images = [image for image in same["images"] if image["role"] == "source_next"]
    assert source_images[0]["text"].startswith("For same intent: ")
    assert source_images[0]["source"] == {"kind": "observation", "run": "record/rec-1", "step": 2}
    variant = items["branches/br-variant@1"]
    assert variant["questions"] == ["reasonable", "executed_as_intended"]
    assert set(variant["suggestion"]["answers"]) == {"reasonable", "executed_as_intended"}
    assert not [
        field
        for field in variant["fields"]
        if "original" in field["text"] or "rule" in field["text"]
    ]
    assert [image["role"] for image in variant["images"]] == ["checkpoint", "branch_next"]
    assert [link["run"] for link in variant["links"]] == ["branches/br-variant"]


def test_a_fork_without_a_valid_reply_keeps_its_rule_verdict(world: World, tmp_path: Path) -> None:
    records, path = _judge_forks(world, tmp_path, ScriptedTransport(lambda request: "{}"))
    same = next(record for record in records if record["branch_run"] == "branches/br-same")
    assert same["status"] == "rules_only" and same["rule"]["verdict"] == "same"
    assert same["llm"] is None and same["llm_errors"]["quality"] is not None
    assert len(same["llm_calls"]) == 6  # both requests, three attempts each
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
    assert len((out / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 5


def test_cli_stops_when_the_route_refuses_the_probe(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = ScriptedTransport(
        lambda request: TransportError("bad", status=400, body="response_format not supported")
    )
    out = tmp_path / "forks"
    code = run(
        ["judge-forks", "--root", str(world.root), "--out", str(out)],
        transport_factory=lambda: transport,
    )
    assert code == 2 and len(transport.requests) == 1 and transport.closed
    assert "response_format not supported" in capsys.readouterr().err
    assert not (out / "judgments.jsonl").exists() and not (out / "report.md").exists()
