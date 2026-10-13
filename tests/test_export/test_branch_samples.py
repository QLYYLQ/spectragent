"""Branch runs as samples (arch §4.15 "分叉样本"; PRD §11 Q2: a neutral record).

A branch run contributes its policy steps; its context is its source run's
prefix (rendered from the source trace, the same translation and segment
rules); it is labelled by its source run's verdicts; branches of one
checkpoint whose targets coincide are flagged ``duplicate_of``, never removed;
and the whole group shares its source task's split. Every trace is committed
by the real ``WALWriter``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from spectragent.core.composer import ConfigComposer
from spectragent.schemas.dataset import DatasetManifest
from spectragent.schemas.restoration import BranchProvenance, RestorationCheck
from tests._helpers.export_runs import exporter_entry, read_jsonl, run_export
from tests._helpers.export_traces import (
    ExportStep,
    click_at,
    screenshot,
    ui_tars_step,
    write_ui_tars_run,
)
from tests._helpers.trace_builder import eval_result

PASSED = {"judge": eval_result(evaluator_id="online_mind2web", predicted_label=1)}
TERMINAL = screenshot(color=99)


def _provenance(index: int, *, step: int = 1) -> BranchProvenance:
    return BranchProvenance(
        source_run_id="src",
        source_trace="src",
        source_benchmark="export_test",
        source_subset="test",
        source_task_id="task-001",
        checkpoint_step=step,
        branch_index=index,
        branch_group_id=f"src/s{step}",
    )


def _branch(root: Path, index: int, first_click: tuple[int, int], *, terminal: bool = True) -> Path:
    steps = [
        ExportStep(
            actions=[click_at(100, 100)], origin="restored", fragments=[screenshot(color=50)]
        ),
        ui_tars_step(click_at(*first_click), thought=f"branch {index}", color=60 + index),
        ui_tars_step(click_at(300, 300), thought="continue", color=70 + index),
    ]
    if terminal:
        steps.append(ExportStep(actions=[], origin="terminal", fragments=[TERMINAL]))
    return write_ui_tars_run(root, f"src-s1-b{index}", steps, branch=_provenance(index))


def _sources(root: Path) -> None:
    write_ui_tars_run(
        root,
        "src",
        [
            ui_tars_step(click_at(100, 100), thought="source zero", color=10),
            ui_tars_step(click_at(200, 200), thought="source one", color=20),
        ],
        verdicts=PASSED,
    )
    _branch(root, 0, (640, 360))
    _branch(root, 1, (640, 360))  # the same decision at the checkpoint
    _branch(root, 2, (10, 10), terminal=False)


def _lines(out: Path) -> list[dict]:
    return [line for path in sorted(out.glob("*.samples.jsonl")) for line in read_jsonl(path)]


def _export(
    composer: ConfigComposer, tmp_path: Path, **overrides: object
) -> tuple[Path, DatasetManifest]:
    _sources(tmp_path / "src")
    out = tmp_path / "out"
    report = run_export(composer, exporter_entry(tmp_path / "src", out, **overrides))
    return out, report.manifest


def test_only_policy_steps(composer: ConfigComposer, tmp_path: Path) -> None:
    out, _manifest = _export(composer, tmp_path)
    by_run: dict[str, list[int]] = {}
    for line in _lines(out):
        sample = line["sample"]
        by_run.setdefault(sample["run_id"], []).append(sample["step"])
    assert {run: sorted(steps) for run, steps in by_run.items()} == {
        "src": [0, 1],
        "src-s1-b0": [1, 2],
        "src-s1-b1": [1, 2],
        "src-s1-b2": [1, 2],
    }


def test_the_context_is_the_source_runs_prefix(composer: ConfigComposer, tmp_path: Path) -> None:
    out, _manifest = _export(composer, tmp_path, split={"validation_fraction": 0.0})
    records = {line["id"]: line for line in read_jsonl(out / "train.jsonl")}
    renders = {line["sample"]["sample_id"]: line["render"] for line in _lines(out)}
    checkpoint = records["src-s1-b0/s1"]["messages"]
    assert [m["role"] for m in checkpoint] == ["system", "user", "assistant", "user", "assistant"]
    assert checkpoint[2]["content"] == records["src/s0"]["messages"][-1]["content"]
    # The step-0 observation is the source run's (color 10), not the branch's restored one.
    assert checkpoint[1]["content"][1] == records["src/s0"]["messages"][1]["content"][1]
    assert renders["src-s1-b0/s1"]["context_steps"] == [0]
    assert renders["src-s1-b0/s2"]["context_steps"] == [0, 1]
    assert checkpoint[-1]["content"].startswith("Thought: branch 0\n")


def test_metadata_and_terminal_ref(composer: ConfigComposer, tmp_path: Path) -> None:
    out, _manifest = _export(composer, tmp_path)
    samples = {line["sample"]["sample_id"]: line["sample"] for line in _lines(out)}
    assert TERMINAL.image_bytes is not None
    branch = samples["src-s1-b0/s1"]["branch"]
    assert branch == {
        "branch_group_id": "src/s1",
        "source_run_id": "src",
        "checkpoint_step": 1,
        "branch_index": 0,
        "terminal_step": 3,
        "terminal_image_sha256": hashlib.sha256(TERMINAL.image_bytes).hexdigest(),
        "duplicate_of": None,
    }
    assert samples["src-s1-b2/s1"]["branch"]["terminal_step"] is None
    assert samples["src/s0"]["branch"] is None
    # Labelled by the source run's verdicts (a branch run has no evals of its own).
    assert [v["verdict_slug"] for v in samples["src-s1-b1/s2"]["labels"]["verdicts"]] == ["judge"]


def test_duplicates_flagged_not_removed(composer: ConfigComposer, tmp_path: Path) -> None:
    out, manifest = _export(composer, tmp_path)
    samples = {line["sample"]["sample_id"]: line["sample"] for line in _lines(out)}
    assert samples["src-s1-b0/s1"]["branch"]["duplicate_of"] is None
    assert samples["src-s1-b1/s1"]["branch"]["duplicate_of"] == "src-s1-b0/s1"
    assert samples["src-s1-b2/s1"]["branch"]["duplicate_of"] is None  # another decision
    # Continuation steps are not compared: only the checkpoint step is the group's question.
    assert samples["src-s1-b1/s2"]["branch"]["duplicate_of"] is None
    assert manifest.duplicates == 1
    assert len(samples) == 8, "flagged, never removed"


def test_group_in_one_split(composer: ConfigComposer, tmp_path: Path) -> None:
    for fraction in (0.0, 1.0, 0.5):
        target = tmp_path / str(fraction)
        out, manifest = _export(
            composer, target, split={"by": "task", "validation_fraction": fraction}
        )
        splits = {line["render"]["split"] for line in _lines(out)}
        assert len(splits) == 1, "source run and its branches share the source task's split"
        assert list(manifest.split.keys) == ["export_test/test/task-001"]


def test_require_eval_reads_source_run(composer: ConfigComposer, tmp_path: Path) -> None:
    _sources(tmp_path / "src")
    write_ui_tars_run(  # a source whose judge said no, and its branch
        tmp_path / "src",
        "bad",
        [ui_tars_step(click_at(1, 1)), ui_tars_step(click_at(2, 2))],
        verdicts={"judge": eval_result(evaluator_id="online_mind2web", predicted_label=0)},
        task_id="task-002",
    )
    write_ui_tars_run(
        tmp_path / "src",
        "bad-s1-b0",
        [
            ExportStep(actions=[click_at(1, 1)], origin="restored", fragments=[screenshot()]),
            ui_tars_step(click_at(3, 3)),
        ],
        branch=_provenance(0).model_copy(
            update={"source_run_id": "bad", "source_trace": "bad", "branch_group_id": "bad/s1"}
        ),
    )
    entry = exporter_entry(
        tmp_path / "src",
        tmp_path / "out",
        filters={
            "require_eval": {"evaluator_id": "online_mind2web"},
            "max_document_misses": "unlimited",
        },
    )
    report = run_export(composer, entry)
    exported = [s.path for s in report.manifest.sources.exported]
    assert exported == ["src", "src-s1-b0", "src-s1-b1", "src-s1-b2"]
    assert report.manifest.sources.filtered == {
        "bad": (
            "it has no graded verdict from 'online_mind2web' with predicted_label 1 (require_eval)"
        ),
        "bad-s1-b0": (
            "its source run bad has no graded verdict from 'online_mind2web' with "
            "predicted_label 1 (require_eval)"
        ),
    }


def test_a_warned_restoration_leaves_the_branch_out(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    """A branch that went on after a failed check (``verify: warn``) contributes nothing by default.

    Its checkpoint sample's context would be the source run's prefix — a state
    the restored browser did not reach — so ``exclude_restoration_diverged``
    (default true) leaves the whole run out; switched off, its policy step is a
    sample like any other branch's.
    """
    _sources(tmp_path / "src")
    warned = RestorationCheck(
        step=0, verdict="warn", reason="url_mismatch", reasons=["url_mismatch"]
    )
    write_ui_tars_run(
        tmp_path / "src",
        "src-s1-b3",
        [
            ExportStep(
                actions=[click_at(100, 100)],
                origin="restored",
                fragments=[screenshot(color=50)],
                restoration=warned,
            ),
            ui_tars_step(click_at(500, 500), thought="branch 3", color=63),
        ],
        branch=_provenance(3),
    )
    default = run_export(composer, exporter_entry(tmp_path / "src", tmp_path / "out"))
    assert default.manifest.sources.filtered == {
        "src-s1-b3": (
            "restoration diverged at step(s) 0 (warn: url_mismatch) (exclude_restoration_diverged)"
        )
    }
    assert "src-s1-b3" not in {s.path for s in default.manifest.sources.exported}

    switched_off = exporter_entry(
        tmp_path / "src",
        tmp_path / "kept",
        filters={"exclude_restoration_diverged": False, "max_document_misses": "unlimited"},
    )
    kept = run_export(composer, switched_off)
    exported = {s.path: s.samples for s in kept.manifest.sources.exported}
    assert exported["src-s1-b3"] == 1
    assert kept.manifest.sources.filtered == {}
