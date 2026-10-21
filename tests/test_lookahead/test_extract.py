"""``extract``: one row per observed step, read only through the trace's public boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lookahead.cli import run
from lookahead.extract import all_rows, step_rows
from lookahead.rows import StepRow
from lookahead.runs import LookaheadInputError, open_runs, run_kind
from tests.test_lookahead.conftest import SPANS, TASK, VARIANT, VARIANT_TASK, World


def _rows(world: World) -> dict[tuple[str, int], StepRow]:
    runs = open_runs(world.root)
    return {(row.run, row.step): row for row in all_rows(runs)}


def test_runs_are_classified_by_recorded_facts_not_paths(world: World) -> None:
    runs = open_runs(world.root)
    kinds = {run.key: run.kind for run in runs.runs}
    assert kinds == {
        "branches/br-ended": "branch",
        "branches/br-offpage": "branch",
        "branches/br-same": "branch",
        "branches/br-typed": "branch",
        "branches/br-unparsed": "branch",
        "branches/br-variant": "branch",
        "record/rec-1": "record",
        "record/rec-2": "record",
        "replays/replay-1": "replay",
    }


def test_a_run_without_composed_configuration_is_a_replay_when_its_setup_names_an_archive(
    world: World,
) -> None:
    config = (
        open_runs(world.root).runs[0].config.model_copy(update={"resolved": {}, "branch": None})
    )
    assert run_kind(config) == "record"
    setup = config.task_setup
    assert setup is not None
    replaying = setup.model_copy(update={"context": {**setup.context, "replay_archive": "/x"}})
    assert run_kind(config.model_copy(update={"task_setup": replaying})) == "replay"


def test_policy_and_restored_steps_get_rows_terminal_steps_only_as_next(world: World) -> None:
    rows = _rows(world)
    assert sorted(step for run, step in rows if run == "record/rec-1") == [0, 1, 2, 3]
    last = rows[("record/rec-1", 3)]
    assert last.next.exists and last.next.step == 4 and last.next.origin == "terminal"
    assert rows[("branches/br-same", 0)].origin == "restored"


def test_a_run_ending_in_an_error_has_no_next_observation(world: World) -> None:
    rows = _rows(world)
    # rec-2's click raised and the run ended there: no observation after it
    failed = rows[("record/rec-2", 1)]
    assert failed.run_outcome == "error"
    assert failed.error_phase == "act" and failed.actions[0].success is False
    assert failed.next.exists is False and failed.next.step is None
    assert failed.raw_dom.next is False
    # the step before it does have a next observation: the failing step's own
    before = rows[("record/rec-2", 0)]
    assert before.next.exists and before.next.step == 1
    # a run that ended normally records the terminal observation after its last step
    last = rows[("record/rec-1", 3)]
    assert last.run_outcome == "done" and last.next.exists and last.next.origin == "terminal"
    # a run that wrote no result says so
    assert rows[("replays/replay-1", 0)].run_outcome is None


def test_row_carries_settle_capture_state_actions_and_effect_facts(world: World) -> None:
    row = _rows(world)[("record/rec-1", 0)]
    assert row.kind == "record" and row.site == "shop.example" and row.task == TASK
    assert row.observation.screenshot and row.observation.url == "https://shop.example/"
    assert row.settle is not None and row.settle.outcome == "settled"
    assert row.capture_state is not None and row.capture_state["pointer"]["status"] == "known"
    click, wait = row.actions
    assert click.name == "browser.click_coordinate" and click.success is True
    assert click.facts["hit"] == {"role": "textbox", "name": "Search"}
    assert click.facts["point"] == [20, 10]
    assert click.effect_text is not None and click.effect_text.startswith("Clicked at (20, 10)")
    assert wait.effect == {"effect_id": "browser.wait", "waited_ms": 1000}
    assert row.next.url == "https://shop.example/search"


def test_scroll_and_typed_text_facts(world: World) -> None:
    rows = _rows(world)
    scroll = rows[("record/rec-1", 2)].actions[0]
    assert scroll.facts["scroll_before"] == [0, 0] and scroll.facts["scroll_after"] == [0, 400]
    typed = rows[("branches/br-typed", 1)].actions[0]
    assert typed.facts["text"] == "耳机"
    assert typed.facts["target"] == {"role": "textbox", "name": "Search"}


def test_raw_dom_is_known_for_both_observations(world: World) -> None:
    rows = _rows(world)
    assert rows[("record/rec-1", 0)].raw_dom.model_dump() == {"current": True, "next": True}
    assert rows[("record/rec-1", 2)].raw_dom.model_dump() == {"current": False, "next": False}
    assert rows[("record/rec-1", 3)].raw_dom.model_dump() == {"current": False, "next": True}


def test_frames_follow_their_observation_by_cycle_wherever_they_were_committed(
    world: World,
) -> None:
    rows = _rows(world)
    first = rows[("record/rec-1", 0)]
    assert first.observation_cycle == 1
    assert [(f.kind, f.action_index, f.batch_step) for f in first.frames] == [
        ("after_observation", None, 0),
        ("before_action", 0, 0),
        ("before_action", 1, 0),
    ]
    # step 1's delayed frame was still in flight and landed in step 2's batch
    second = rows[("record/rec-1", 1)]
    delayed = second.frame("after_observation")
    assert delayed is not None and delayed.batch_step == 2 and delayed.cycle == 2
    # step 2: no raw DOM, so the cycle comes from the frames taken before its actions
    third = rows[("record/rec-1", 2)]
    assert third.observation_cycle == 3
    superseded = third.frame("after_observation")
    assert superseded is not None and superseded.outcome == "superseded" and not superseded.captured
    # step 3: only a delayed frame, placed by its request time
    assert rows[("record/rec-1", 3)].observation_cycle == 4


def test_before_action_frames_join_their_env_act_call_through_the_spans(world: World) -> None:
    first = _rows(world)[("record/rec-1", 0)]
    joined = {
        frame.action_index: frame.span for frame in first.frames if frame.kind == "before_action"
    }
    assert joined == {0: SPANS[0], 1: SPANS[1]}
    delayed = first.frame("after_observation")
    assert delayed is not None and delayed.span is None
    assert all(frame.released_at is None for frame in first.frames)  # the reader gives none yet
    # spans measured, no env.act call: nothing to join
    assert all(frame.span is None for frame in _rows(world)[("record/rec-1", 1)].frames)


def test_a_run_without_frames_has_none(world: World) -> None:
    rows = _rows(world)
    assert rows[("record/rec-2", 0)].frames == []
    assert rows[("record/rec-2", 0)].observation_cycle is None


def test_action_spans_are_null_when_not_measured_and_copied_when_recorded(world: World) -> None:
    rows = _rows(world)
    assert rows[("record/rec-1", 0)].action_spans == SPANS
    assert rows[("record/rec-1", 1)].action_spans == []
    assert rows[("record/rec-2", 0)].action_spans is None


def test_branch_rows_carry_provenance_restoration_and_the_source_key(world: World) -> None:
    rows = _rows(world)
    restored = rows[("branches/br-same", 0)]
    assert restored.restoration is not None and restored.restoration.verdict == "ok"
    assert restored.restoration.screenshot_distance == 1
    branch = rows[("branches/br-same", 1)].branch
    assert branch is not None
    assert branch.source_run == "record/rec-1" and branch.checkpoint_step == 1
    assert branch.source_task == TASK
    assert branch.restoration.restored_steps == 1
    assert branch.restoration.checkpoint is not None
    assert branch.restoration.verdicts == {"ok": 2}
    ended = rows[("branches/br-ended", 0)].branch
    assert ended is not None and ended.restoration.first_problem_step == 0


def test_task_variant_provenance_is_carried(world: World) -> None:
    rows = _rows(world)
    row = rows[("branches/br-variant", 1)]
    assert row.task == VARIANT_TASK
    assert row.task_variant == VARIANT
    assert rows[("branches/br-same", 1)].task_variant is None


def test_keys_are_relative_to_the_viewer_root(world: World) -> None:
    runs = open_runs(world.root / "record", world.root)
    assert sorted(run.key for run in runs.runs) == ["record/rec-1", "record/rec-2"]
    rows = list(step_rows(runs.runs[0], runs))
    assert rows[0].run == "record/rec-1"


def test_a_root_outside_the_viewer_root_is_refused(world: World, tmp_path: Path) -> None:
    with pytest.raises(LookaheadInputError, match="viewer root"):
        open_runs(world.root, tmp_path)


def test_an_unreadable_run_is_skipped_with_its_reason(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "manifest.json").write_text('{"samples": [], "exporter": {}}', encoding="utf-8")
    runs = open_runs(tmp_path)
    assert runs.runs == []
    assert list(runs.skipped) == ["broken"]
    assert "manifest.json does not read back" in runs.skipped["broken"]


def test_cli_writes_one_json_line_per_row(world: World, tmp_path: Path) -> None:
    out = tmp_path / "rows.jsonl"
    assert run(["extract", "--root", str(world.root), "--out", str(out)]) == 0
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert {line["format"] for line in lines} == {"lookahead-step-1"}
    assert len(lines) == len(_rows(world))
