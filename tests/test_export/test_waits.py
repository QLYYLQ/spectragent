"""``wait_actions: drop_when_capped`` (arch §4.15 "等待动作").

On a step whose capture state says the settle wait was capped, the export
removes the waits the teacher ran (a step with nothing else leaves the
trajectory and later samples' history), rewrites the thoughts that removal
affects with the exporter's helper chat model, records what it did, and keeps
the export deterministic through a content-addressed rewrite cache. Every
source is committed by the real ``WALWriter``; the student is the shipped
UI-TARS one; the helper model is a scripted fake, so nothing reaches a
network.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.actions._registrar import ActionRegistrar, is_wait_action
from spectragent.core.composer import ConfigComposer, InstantiationError
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.trace.digest import tree_digest
from spectragent.core.trace.models import TraceNegotiation
from spectragent.core.trace.reader import RunTrace
from spectragent.export.cli import main
from spectragent.export.errors import ExportConfigError, ThoughtRewriteCacheError
from spectragent.export.runner import ExportReport, ExportRunner
from spectragent.export.waits import (
    REWRITE_PROMPT_DIGEST,
    REWRITE_PROMPT_VERSION,
    capped_waits,
)
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.dataset import DatasetManifest
from spectragent.schemas.restoration import BranchProvenance
from spectragent.schemas.step_record import StepRecord
from tests._helpers.export_runs import exporter_entry, read_jsonl, run_export
from tests._helpers.export_traces import (
    NEVER_QUIET,
    UI_TARS_NEGOTIATION,
    ExportStep,
    capture_state,
    click_at,
    finished,
    screenshot,
    write_ui_tars_run,
)

WAIT = Action(name="browser.wait", params={})
#: The rewriter's identity in the YAML the tests compose (its calls are scripted).
REWRITER_BLOCK = {
    "model_type": "openai",
    "model": "thought-rewriter",
    "provider": {"name": "direct"},
}


class ScriptedRewriter:
    """A chat model that removes every sentence mentioning waiting or loading.

    It reads the thought from the request's last lines, as the prompt lays it
    out, and remembers every request; nothing leaves the process.
    """

    inference_kind = "chat"
    model = "thought-rewriter"
    model_type = "openai"
    provider = "direct"
    name = "scripted thought rewriter"

    def __init__(self) -> None:
        self.requests: list[list[Any]] = []
        self.closed = 0

    async def ainvoke(self, messages: Any, output_format: Any = None, **kwargs: Any) -> Any:
        assert output_format is None
        self.requests.append(list(messages))
        user = messages[-1]
        assert isinstance(user, UserMessage) and isinstance(user.content, str)
        thought = user.content.split("Thought:\n", 1)[1]
        kept = [
            sentence
            for sentence in re.split(r"(?<=[.!?])\s+", thought)
            if not re.search(r"wait|load", sentence, re.IGNORECASE)
        ]
        return ChatInvokeCompletion(completion=" ".join(kept), usage=None)

    async def aclose(self, *, timeout: float = 5.0) -> tuple:
        self.closed += 1
        return ()


def _step(
    actions: list[Action],
    thought: str,
    *,
    capped: bool = False,
    color: int = 200,
    **kwargs: Any,
) -> ExportStep:
    """A UI-TARS step whose capture was settled, or capped."""
    return ExportStep(
        actions=actions,
        llm_output=f"Thought: {thought}\nAction: (recorded reply)",
        fragments=[
            screenshot(color=color),
            capture_state(NEVER_QUIET) if capped else capture_state(),
        ],
        **kwargs,
    )


#: One recorded run: a wait-only step on a capped capture (1), the step after
#: it (2), two steps that waited and acted on a capped capture (3, 4) and a
#: wait on a settled capture (5), which stays.
STEPS = [
    _step([click_at(100, 100)], "Open the menu.", color=10),
    _step([WAIT], "The page is still loading, so I wait.", capped=True, color=11),
    _step([click_at(200, 200)], "The page has loaded now. Pick the kettle.", color=12),
    _step([WAIT, click_at(300, 300)], "Still loading. Add it to the cart.", capped=True, color=13),
    _step([WAIT, click_at(400, 400)], "Close the dialog.", capped=True, color=14),
    _step([WAIT], "Give the banner a second.", color=15),
    _step([finished("added")], "Done.", color=16),
]


def _sources(root: Path) -> Path:
    return write_ui_tars_run(root, "run-a", STEPS, task_id="a")


def _entry(src: Path, out: Path, cache: Path | None = None, **overrides: Any) -> dict[str, Any]:
    entry = exporter_entry(src, out, split={"validation_fraction": 0.0}, **overrides)
    entry.setdefault("wait_actions", "drop_when_capped")
    if entry["wait_actions"] == "drop_when_capped" and entry.get("thought_source") != "none":
        entry.setdefault("thought_rewrite_llms", [copy.deepcopy(REWRITER_BLOCK)])
        entry.setdefault("thought_rewrite_cache", str(cache))
    return entry


def _export(
    composer: ConfigComposer, entry: dict[str, Any], model: ScriptedRewriter | None
) -> ExportReport:
    """Compose *entry* as the CLI does; its rewriter, if any, replaced by *model*."""
    run = composer.compose({"exporter": [copy.deepcopy(entry)]})
    exporter = run.single("exporter")
    if model is not None:
        assert len(exporter.thought_rewrite_llms) == 1
        exporter.thought_rewrite_llms = [model]
    return ExportRunner.from_run(run, composer=composer, origin="the test document").run()


def _lines(out: Path) -> dict[str, dict[str, Any]]:
    return {line["sample"]["sample_id"]: line for line in read_jsonl(out / "train.samples.jsonl")}


def _records(out: Path) -> dict[str, dict[str, Any]]:
    return {line["id"]: line for line in read_jsonl(out / "train.jsonl")}


def _texts(message: dict[str, Any]) -> str:
    content = message["content"]
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content if part.get("type") == "text")


# ── what counts as a wait, and which waits go ──────────────────


def test_only_the_browser_wait_declares_that_it_only_waits() -> None:
    ours = {
        key
        for key, cls in ActionRegistrar.get_all().items()
        if cls.__module__.startswith("spectragent.")
    }
    assert {key for key in ours if is_wait_action(key)} == {"browser.wait"}
    assert not is_wait_action("wait")  # a short name is not what a trace records
    assert not is_wait_action("nothing.registered")


class TestWhichWaitsGo:
    def test_wait_only_and_mixed_steps_on_capped_captures_only(self, tmp_path: Path) -> None:
        trace = RunTrace(_sources(tmp_path))
        waits = capped_waits(trace)
        assert sorted(waits) == [1, 3, 4]
        assert waits[1].removes_step
        assert [(r.step, r.action_index, r.action) for r in waits[1].removed] == [(1, 0, WAIT)]
        for step, click in ((3, click_at(300, 300)), (4, click_at(400, 400))):
            assert not waits[step].removes_step
            assert waits[step].record.actions == [click]
            assert len(waits[step].record.action_results) == 1
            assert [r.action_index for r in waits[step].removed] == [0]
        # The recorded trace is only read.
        recorded = trace.step(3)
        assert recorded is not None and recorded.actions == [WAIT, click_at(300, 300)]

    def test_a_failed_wait_stays_and_so_does_the_skipped_tail_after_it(
        self, tmp_path: Path
    ) -> None:
        failed = ActionResult(success=False, error="seconds=90 exceeds max_seconds=30")
        run = write_ui_tars_run(
            tmp_path,
            "run-f",
            [
                # A wait that failed, then a click the Loop skipped.
                _step(
                    [WAIT, click_at(1, 1)],
                    "wait long",
                    capped=True,
                    results=[failed],
                    skipped_actions=1,
                ),
                # A wait that ran, a click that failed, a wait in the skipped tail.
                _step(
                    [WAIT, click_at(2, 2), WAIT],
                    "wait, then click",
                    capped=True,
                    results=[ActionResult(success=True), ActionResult(success=False, error="x")],
                    skipped_actions=1,
                ),
            ],
        )
        waits = capped_waits(RunTrace(run))
        assert 0 not in waits
        reduced = waits[1].record
        assert [r.action_index for r in waits[1].removed] == [0]
        assert reduced.actions == [click_at(2, 2), WAIT]
        assert [r.success for r in reduced.action_results] == [False]
        assert reduced.skipped_actions == 1 and reduced.skipped() == [WAIT]
        StepRecord.model_validate(reduced.model_dump())  # the invariant still holds

    def test_no_capture_state_no_change(self, tmp_path: Path) -> None:
        """Not negotiated, failed, or no settle wait recorded: no evidence, nothing removed."""
        without = UI_TARS_NEGOTIATION.model_copy(
            update={
                "active_fragments": [
                    k for k in UI_TARS_NEGOTIATION.active_fragments if k != "browser.capture_state"
                ],
                "env_sourced_fragments": [
                    k
                    for k in UI_TARS_NEGOTIATION.env_sourced_fragments
                    if k != "browser.capture_state"
                ],
            }
        )
        assert isinstance(without, TraceNegotiation)
        not_negotiated = write_ui_tars_run(
            tmp_path,
            "run-n",
            [ExportStep(actions=[WAIT], fragments=[screenshot()])],
            negotiation=without,
        )
        no_wait = capture_state()
        no_wait.settle = None  # the settle wait was turned off
        other = write_ui_tars_run(
            tmp_path,
            "run-o",
            [
                ExportStep(
                    actions=[WAIT], fragments=[screenshot()], failed=["browser.capture_state"]
                ),
                ExportStep(actions=[WAIT], fragments=[screenshot(), no_wait]),
            ],
        )
        assert capped_waits(RunTrace(not_negotiated)) == {}
        assert capped_waits(RunTrace(other)) == {}


# ── the export ───────────────────────────────────────────────


class TestExport:
    def test_removal_history_rewrite_and_provenance(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        src = _sources(tmp_path / "src").parent
        model = ScriptedRewriter()
        out = tmp_path / "out"
        report = _export(composer, _entry(src, out, tmp_path / "cache"), model)
        lines = _lines(out)
        assert sorted(lines) == [
            "run-a/s0",
            "run-a/s2",
            "run-a/s3",
            "run-a/s4",
            "run-a/s5",
            "run-a/s6",
        ]

        # The wait-only step left the trajectory: no sample, not in anyone's history.
        after = lines["run-a/s2"]
        assert after["sample"]["history_steps"] == [0]
        assert after["render"]["context_steps"] == [0]
        assert lines["run-a/s6"]["sample"]["history_steps"] == [0, 2, 3, 4, 5]
        # (ii) the first step kept after it: its thought rewritten, synthetic.
        removal = after["sample"]["wait_removal"]
        assert removal["removed_actions"] == []
        assert removal["removed_steps_before"] == [
            {
                "run_id": "run-a",
                "step": 1,
                "action_index": 0,
                "action": WAIT.model_dump(mode="json"),
            }
        ]
        thought = removal["thought"]
        assert thought["original"] == "The page has loaded now. Pick the kettle."
        assert thought["rewritten"] == "Pick the kettle."
        assert (thought["outcome"], thought["synthetic"]) == ("rewritten", True)
        assert thought["model"] == {
            "model_type": "openai",
            "provider": "direct",
            "model": "thought-rewriter",
        }
        assert (thought["prompt_version"], thought["prompt_digest"]) == (
            REWRITE_PROMPT_VERSION,
            REWRITE_PROMPT_DIGEST,
        )
        assert after["sample"]["thought"]["text"] == "Pick the kettle."
        assert after["render"]["target"].startswith("Thought: Pick the kettle.\n")

        # (i) a step that kept its click: the wait is gone from its target.
        mixed = lines["run-a/s3"]
        assert mixed["sample"]["teacher_actions"] == [click_at(300, 300).model_dump(mode="json")]
        assert [a["name"] for a in mixed["render"]["target_actions"]] == [
            "browser.click_coordinate"
        ]
        assert "wait" not in mixed["render"]["target"]
        assert mixed["sample"]["wait_removal"]["removed_actions"][0]["action_index"] == 0
        assert mixed["sample"]["wait_removal"]["thought"]["rewritten"] == "Add it to the cart."
        # A thought with nothing about waiting comes back as it was: not synthetic.
        unchanged = lines["run-a/s4"]["sample"]["wait_removal"]["thought"]
        assert (unchanged["outcome"], unchanged["synthetic"]) == ("reviewed_unchanged", False)
        assert unchanged["rewritten"] == unchanged["original"] == "Close the dialog."
        # A wait on a settled capture is the teacher's own choice: it stays.
        settled = lines["run-a/s5"]
        assert settled["sample"]["wait_removal"] is None
        assert [a["name"] for a in settled["sample"]["teacher_actions"]] == ["browser.wait"]
        assert lines["run-a/s0"]["sample"]["wait_removal"] is None
        assert lines["run-a/s6"]["sample"]["wait_removal"] is None

        manifest = report.manifest
        ledger = manifest.wait_actions
        assert ledger.policy == "drop_when_capped"
        assert [(e.source, e.run_id, e.step, e.action_indices) for e in ledger.removed_steps] == [
            ("run-a", "run-a", 1, [0])
        ]
        assert [(e.run_id, e.step, e.action_index) for e in ledger.removed_actions] == [
            ("run-a", 3, 0),
            ("run-a", 4, 0),
        ]
        assert (ledger.rewritten, ledger.reviewed_unchanged) == (2, 1)
        assert ledger.rewriter is not None
        assert (ledger.rewriter.model, ledger.rewriter.prompt_digest) == (
            "thought-rewriter",
            REWRITE_PROMPT_DIGEST,
        )
        assert manifest.exporter["wait_actions"] == "drop_when_capped"
        assert "cache" not in json.dumps(manifest.exporter)  # the cache path is a path knob
        assert report.thought_rewrites == {"cache_hits": 0, "model_calls": 3}
        assert len(model.requests) == 3 and model.closed == 1

    def test_the_context_reads_as_if_the_removed_step_never_happened(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        src = _sources(tmp_path / "src").parent
        out = tmp_path / "out"
        _export(composer, _entry(src, out, tmp_path / "cache"), ScriptedRewriter())
        records = _records(out)
        after = records["run-a/s2"]["messages"]
        # Seed, step 0's turn and reply, then step 2's observation: no turn for step 1.
        assert [m["role"] for m in after] == [
            "system",
            "system",
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert sum(1 for m in after for p in m.get("content") or [] if _is_image(p)) == 2
        # The student is shown step numbers as if step 1 had never been taken.
        assert "Step: 2/30" in _texts(after[4]) and "Step: 3/30" not in _texts(after[4])
        mixed = records["run-a/s3"]["messages"]
        assert "Step: 3/30" in _texts(mixed[-2])
        # The rewritten thought is what the next samples' history shows.
        assert mixed[5]["content"].startswith("Thought: Pick the kettle.\n")
        assert all("loaded" not in _texts(m) for m in mixed if m["role"] == "assistant")

    def test_a_re_export_reads_the_cache_and_writes_the_same_bytes(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        src = _sources(tmp_path / "src").parent
        source_before = tree_digest(src, metadata=True)
        cache = tmp_path / "cache"
        first_model, second_model = ScriptedRewriter(), ScriptedRewriter()
        first = _export(composer, _entry(src, tmp_path / "out1", cache), first_model)
        second = _export(composer, _entry(src, tmp_path / "out2", cache), second_model)
        assert first.thought_rewrites == {"cache_hits": 0, "model_calls": 3}
        assert second.thought_rewrites == {"cache_hits": 3, "model_calls": 0}
        assert second_model.requests == []
        assert tree_digest(tmp_path / "out1") == tree_digest(tmp_path / "out2")
        assert first.manifest == second.manifest
        # Three entries, each named by its key and recording what it stands for.
        entries = sorted(cache.glob("*.json"))
        assert len(entries) == 3
        keys = {
            line["sample"]["wait_removal"]["thought"]["cache_key"]
            for line in _lines(tmp_path / "out1").values()
            if line["sample"]["wait_removal"] and line["sample"]["wait_removal"]["thought"]
        }
        assert {path.stem for path in entries} == keys
        # The rewriting never touched the recorded runs.
        assert tree_digest(src, metadata=True) == source_before

    def test_a_cache_entry_that_does_not_fit_is_refused(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        src = _sources(tmp_path / "src").parent
        cache = tmp_path / "cache"
        _export(composer, _entry(src, tmp_path / "out1", cache), ScriptedRewriter())
        entry = sorted(cache.glob("*.json"))[0]
        data = json.loads(entry.read_text())
        data["inputs"]["thought"] = "another thought"
        entry.write_text(json.dumps(data))
        with pytest.raises(ThoughtRewriteCacheError, match=entry.name):
            _export(composer, _entry(src, tmp_path / "out2", cache), ScriptedRewriter())
        assert not (tmp_path / "out2").exists()

    def test_keep_changes_nothing_and_calls_no_model(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        src = _sources(tmp_path / "src").parent
        out = tmp_path / "out"
        report = run_export(composer, _entry(src, out, wait_actions="keep"))
        lines = _lines(out)
        assert len(lines) == len(STEPS)
        assert all(line["sample"]["wait_removal"] is None for line in lines.values())
        assert lines["run-a/s1"]["render"]["target_actions"][0]["name"] == "browser.wait"
        ledger = report.manifest.wait_actions
        assert (ledger.policy, ledger.removed_steps, ledger.removed_actions) == ("keep", [], [])
        assert (ledger.rewritten, ledger.reviewed_unchanged, ledger.rewriter) == (0, 0, None)
        assert report.thought_rewrites == {}

    def test_without_teacher_thoughts_waits_go_and_no_model_is_needed(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        src = _sources(tmp_path / "src").parent
        out = tmp_path / "out"
        report = _export(composer, _entry(src, out, thought_source="none"), None)
        lines = _lines(out)
        assert "run-a/s1" not in lines
        removal = lines["run-a/s2"]["sample"]["wait_removal"]
        assert removal["thought"] is None and len(removal["removed_steps_before"]) == 1
        assert report.manifest.wait_actions.rewriter is None
        assert report.thought_rewrites == {}

    def test_a_branch_checkpoint_after_a_removed_source_step(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        """The checkpoint step follows the source run's step t-1: removed, it is rewritten."""
        src = tmp_path / "src"
        write_ui_tars_run(
            src,
            "src",
            [
                _step([click_at(100, 100)], "Open the menu.", color=10),
                _step([WAIT], "Still loading, so I wait.", capped=True, color=11),
                _step([click_at(200, 200)], "Pick the kettle.", color=12),
            ],
        )
        write_ui_tars_run(
            src,
            "src-s2-b0",
            [
                ExportStep(
                    actions=[click_at(100, 100)], origin="restored", fragments=[screenshot()]
                ),
                ExportStep(actions=[WAIT], origin="restored", fragments=[screenshot()]),
                _step([click_at(640, 360)], "It has loaded. Take the other one.", color=60),
            ],
            branch=BranchProvenance(
                source_run_id="src",
                source_trace="src",
                source_benchmark="export_test",
                source_subset="test",
                source_task_id="task-001",
                checkpoint_step=2,
                branch_index=0,
                branch_group_id="src/s2",
            ),
        )
        out = tmp_path / "out"
        _export(composer, _entry(src, out, tmp_path / "cache"), ScriptedRewriter())
        lines = _lines(out)
        checkpoint = lines["src-s2-b0/s2"]
        assert checkpoint["sample"]["history_steps"] == [0]
        assert checkpoint["render"]["context_steps"] == [0]
        removal = checkpoint["sample"]["wait_removal"]
        assert [(r["run_id"], r["step"]) for r in removal["removed_steps_before"]] == [("src", 1)]
        assert removal["thought"]["rewritten"] == "Take the other one."
        context = _records(out)["src-s2-b0/s2"]["messages"]
        assert "Step: 2/30" in _texts(context[-2])


def _is_image(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") == "image_url"


# ── the configuration ────────────────────────────────────────


class TestConfiguration:
    def test_the_yaml_reaches_the_exporter(self, composer: ConfigComposer, tmp_path: Path) -> None:
        entry = _entry(tmp_path, tmp_path / "out", tmp_path / "cache")
        exporter = composer.compose({"exporter": [entry]}).single("exporter")
        assert exporter.wait_actions == "drop_when_capped"
        assert exporter.thought_rewrite_cache == str(tmp_path / "cache")
        (model,) = exporter.thought_rewrite_llms
        assert (model.inference_kind, model.model, model.model_type) == (
            "chat",
            "thought-rewriter",
            "openai",
        )

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"thought_rewrite_llms": None}, "needs thought_rewrite_llms"),
            ({"thought_rewrite_cache": None}, "needs thought_rewrite_cache"),
            (
                {"wait_actions": "keep"},
                "thought_rewrite_llms and thought_rewrite_cache would never be used: "
                "wait_actions is keep",
            ),
            (
                {"wait_actions": "keep", "thought_rewrite_llms": None},
                "thought_rewrite_cache would never be used",
            ),
            ({"thought_source": "none"}, "there is no thought to rewrite"),
        ],
    )
    def test_the_rewrite_fields_follow_the_option_or_the_document_is_refused(
        self,
        composer: ConfigComposer,
        tmp_path: Path,
        overrides: dict[str, Any],
        message: str,
    ) -> None:
        entry = _entry(tmp_path, tmp_path / "out", tmp_path / "cache")
        entry.update(copy.deepcopy(overrides))
        entry = {key: value for key, value in entry.items() if value is not None}
        with pytest.raises(InstantiationError, match=message):
            composer.compose({"exporter": [entry]})

    def test_more_than_one_rewriter_is_refused(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        entry = _entry(tmp_path, tmp_path / "out", tmp_path / "cache")
        entry["thought_rewrite_llms"] = [REWRITER_BLOCK, REWRITER_BLOCK]
        with pytest.raises((ValidationError, InstantiationError)):
            composer.compose({"exporter": [entry]})

    def test_the_runner_checks_what_arrived(self, composer: ConfigComposer, tmp_path: Path) -> None:
        """A receptor filled after construction is checked again before anything runs."""
        entry = _entry(tmp_path, tmp_path / "out", tmp_path / "cache")
        run = composer.compose({"exporter": [entry]})
        run.single("exporter").thought_rewrite_llms = []
        with pytest.raises(ExportConfigError, match="needs thought_rewrite_llms"):
            ExportRunner.from_run(run, composer=composer, origin="the test document").run()
        assert not (tmp_path / "out").exists()


def test_the_manifest_reads_back_through_its_model(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    src = _sources(tmp_path / "src").parent
    out = tmp_path / "out"
    report = _export(composer, _entry(src, out, tmp_path / "cache"), ScriptedRewriter())
    manifest = DatasetManifest.model_validate_json((out / "manifest.json").read_text())
    assert manifest == report.manifest
    assert str(tmp_path) not in (out / "manifest.json").read_text()


def test_a_thought_that_only_spoke_of_waiting_leaves_the_sample_without_one(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    write_ui_tars_run(
        src,
        "run-e",
        [_step([WAIT, click_at(300, 300)], "Still loading, so I wait.", capped=True)],
    )
    out = tmp_path / "out"
    _export(composer, _entry(src, out, tmp_path / "cache"), ScriptedRewriter())
    line = _lines(out)["run-e/s0"]
    thought = line["sample"]["wait_removal"]["thought"]
    assert (thought["rewritten"], thought["outcome"], thought["synthetic"]) == (
        "",
        "rewritten",
        True,
    )
    assert line["sample"]["thought"] is None and line["render"]["thought"] is None
    assert not line["render"]["target"].startswith("Thought:")


def test_the_cli_reads_every_rewrite_from_the_cache(
    composer: ConfigComposer, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A YAML rewriter whose every rewrite is cached: the CLI exports without a model call."""
    src = _sources(tmp_path / "src").parent
    cache = tmp_path / "cache"
    _export(composer, _entry(src, tmp_path / "first", cache), ScriptedRewriter())
    config = tmp_path / "export.yaml"
    config.write_text(yaml.safe_dump({"exporter": [_entry(src, tmp_path / "second", cache)]}))
    assert main(["--config", str(config)]) == 0
    assert "thought rewrites: 3 from the cache, 0 model call(s)" in capsys.readouterr().out
    assert tree_digest(tmp_path / "first") == tree_digest(tmp_path / "second")


def test_a_step_kept_as_text_writes_and_feeds_back_only_what_stayed(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    """``keep_as_text``: the text turn lists the kept actions; the next feedback matches it."""
    src = tmp_path / "src"
    navigate = Action(name="browser.navigate", params={"url": "https://shop.example.test/cart"})
    write_ui_tars_run(
        src,
        "run-t",
        [
            _step([WAIT, navigate], "Still loading. Open the cart.", capped=True, color=20),
            _step([click_at(10, 10)], "Check out.", color=21),
        ],
    )
    out = tmp_path / "out"
    report = _export(
        composer,
        _entry(
            src,
            out,
            tmp_path / "cache",
            translator={"translator_id": "identity", "untranslatable": "keep_as_text"},
        ),
        ScriptedRewriter(),
    )
    assert report.manifest.steps.untranslatable == {"no_counterpart": 1}
    messages = _records(out)["run-t/s1"]["messages"]
    kept = messages[3]["content"]
    assert kept.startswith("[not expressible: no_counterpart] ")
    assert "browser.navigate" in kept and "browser.wait" not in kept
    feedback = _texts(messages[4])
    assert "browser.navigate" in feedback and "browser.wait" not in feedback
    # The step that was no sample is not counted among the exported rewrites.
    assert report.manifest.wait_actions.rewritten == 0
    assert [(e.step, e.action_index) for e in report.manifest.wait_actions.removed_actions] == [
        (0, 0)
    ]
