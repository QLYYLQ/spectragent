"""The training export on fresh recordings from a real Chrome (M32 acceptance, arch §4.15).

Each test records a run on the local fixture site with the real runtime, the
real ``TraceWriter`` and a loopback scripted model (no model spend), then
exports it with the real ``ExportRunner`` and the shipped student
(``configs/flywheel/ui_tars_student.yaml``):

- **identity** — a UI-TARS run recorded with the student's own configuration
  (``normalized_1000``, pinned DPR 1): the rendered context of the *k*-th
  sample equals, message for message in the trace's recorded form
  (``recorded_form``), the prompt of step *k*'s first model call, and each
  target parses to what the step executed. The expected differences, and
  only those: a step's parse re-ask turns (the rejected reply and the
  feedback) are in the recorded prompts of later steps but never in a
  student context (the variant scripts one parse failure and checks that
  exactly those two turns differ); segments a skipped or dropped step starts
  (none here); and the writing of an assistant turn (the student's canonical
  text vs the model's — here the script writes the canonical text);
- **index_to_coordinate, BrowserUse teacher** — a DOM run on the ``sticky``
  page with ``browser.dom_raw`` bound: the clicks on element indices become
  clicks at points, the occlusion check reads the raw DOM's paint order, and
  the link whose centre the sticky header covers is clicked below the header;
- **index_to_coordinate, ``skyvern_hit_test`` recording** — the same flow
  recorded with the hit-testing cleaner and no raw DOM: the page's own hit
  test is the evidence.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.composer import ConfigComposer
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops.restore.source import recorded_form
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.export import canonicalise
from spectragent.export.render import RenderedSample, StudentRenderer, TrajectoryStep
from spectragent.export.translate.identity import IdentityTranslator
from tests._helpers import branch_driver
from tests._helpers import replay_driver as driver
from tests._helpers.export_runs import exporter_entry, read_jsonl, run_export, student_root
from tests._helpers.fixture_site import FixtureSite
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

TASK = "Open the clear link on the sticky page."
#: The UI-TARS script, in the student's canonical writing.
CLICK = "Thought: Open the clear link.\nAction: click(point='(546, 305)')"
WAIT = "Thought: Let the page settle.\nAction: wait()"
FINISH = "Thought: The clear link is open.\nAction: finished(content='done')"
UNPARSEABLE = "Thought: I am not sure what to do yet."


@pytest.fixture(autouse=True)
def _loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _record_ui_tars(
    tmp_path: Path, script: list[str], *, parse_retries: int = 0
) -> tuple[RunTrace, Path]:
    """A UI-TARS run recorded with the student's own configuration and a trace writer."""
    traces = tmp_path / "traces"
    raw = student_root()
    with FixtureSite(mapped=True) as site, ScriptedLLMServer(script) as llm:
        agent = raw["agent"][0]
        agent["agent_loop"]["parse_retries_per_step"] = parse_retries
        agent["llm"].update(
            {
                "model": llm.model,
                "base_url": llm.base_url,
                "api_key": "scripted",
                "max_retries": 0,
            }
        )
        raw["environment"][0]["capabilities"][0]["backend"]["extra_args"] = site.chrome_args()
        raw["hook"] = [{"hook_id": "trace_writer", "trace_dir": str(traces)}]
        run = ConfigComposer(registrars=_collect()).compose(raw)
        (writer,) = [hook for hook in run.instances("hook") if isinstance(hook, TraceWriter)]

        async def execute() -> Any:
            return await AgentRuntime.for_agent(run, "student").execute(
                TASK,
                task_id="identity--0001",
                benchmark="export_live",
                timeout=120,
                env_context={"start_url": site.url("sticky")},
            )

        result = asyncio.run(execute())
    assert result.answer == "done", result
    assert writer.run_dir is not None
    return RunTrace(writer.run_dir), traces


def _collect() -> dict[str, Any]:
    from spectragent.scripts.generate_all import _collect_registrars

    return _collect_registrars()


def _rendered(composer: ConfigComposer, trace: RunTrace) -> list[RenderedSample]:
    """The identity rendering of *trace* by the shipped student, as the runner does it."""
    renderer = StudentRenderer(
        composer.compose(student_root()),
        untranslatable="drop_step",
        thought_source="teacher",
        thought_index_refs="drop",
    )
    renderer.check_source(trace, trace.run_id)
    samples = canonicalise(
        trace, rel_path=trace.run_id, thought_source="teacher", teacher_cls=UITarsAgent
    )
    translator = IdentityTranslator()
    by_step = {s.step: translator.translate(s, trace).applied(s) for s in samples}
    return renderer.render(
        [
            TrajectoryStep(trace, trace.run_id, record, by_step.get(record.step))
            for record in trace.step_records
        ]
    )


def _prompt(trace: RunTrace, step: int, call: int = 0) -> list[Any]:
    return recorded_form(trace.llm_messages(step)[call].messages)


def _same_up_to_writing(
    item: RenderedSample, recorded: list[Any], rendered: list[RenderedSample]
) -> None:
    """*item*'s context is *recorded*, message for message — assistant turns by what they execute.

    Every system and user message is equal in the trace's recorded form. The
    *j*-th assistant turn is the student's reply to step *j* (its canonical
    writing); what that reply executes is checked against the recorded step
    by :func:`_check_targets`, so the two turns agree in what they parse to.
    """
    live = recorded_form(item.context)
    assert [m["role"] for m in live] == [m["role"] for m in recorded]
    assert [m for m in live if m["role"] != "assistant"] == [
        m for m in recorded if m["role"] != "assistant"
    ]
    replies = [m["content"] for m in live if m["role"] == "assistant"]
    assert replies == [rendered[j].target for j in range(len(replies))]


def _check_targets(trace: RunTrace, rendered: list[RenderedSample]) -> None:
    """Each target executes what its recorded step executed."""
    for item in rendered:
        record = trace.step(item.sample.step)
        assert record is not None
        assert [(a.name, a.params) for a in item.target_actions] == [
            (a.name, a.params) for a in record.actions
        ]
        assert item.step_info == "recorded"


def test_identity_export_matches_recorded_inputs(composer: ConfigComposer, tmp_path: Path) -> None:
    trace, traces = _record_ui_tars(tmp_path, [CLICK, WAIT, FINISH])
    assert trace.step_count == 3 and trace.manifest.status.value == "complete"

    rendered = _rendered(composer, trace)
    assert [item.sample.step for item in rendered] == [0, 1, 2]
    _check_targets(trace, rendered)
    for item in rendered:
        _same_up_to_writing(item, _prompt(trace, item.sample.step), rendered)
    # The expected difference in writing: the model wrote y=305 on the grid, which
    # executes at viewport y=220; the student's canonical writing of y=220 is 306.
    first = trace.step(0)
    assert first is not None and first.llm_output == CLICK
    assert rendered[0].target == CLICK.replace("305", "306")
    assert rendered[1].target == WAIT and rendered[2].target == FINISH

    out = tmp_path / "dataset"
    report = run_export(composer, exporter_entry(traces, out))
    assert sum(report.samples.values()) == 3
    assert report.manifest.capture.unchecked == []
    ids = [
        line["id"]
        for path in sorted(out.glob("*.jsonl"))
        if ".samples." not in path.name
        for line in read_jsonl(path)
    ]
    assert sorted(ids) == [f"{trace.run_id}/s{k}" for k in range(3)]
    assert len(list((out / "images").iterdir())) == len({trace.screenshot(k) for k in range(3)}), (
        "one stored image per distinct screenshot"
    )


def test_identity_export_differs_only_by_the_parse_retry_turns(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    trace, _traces = _record_ui_tars(tmp_path, [CLICK, UNPARSEABLE, WAIT, FINISH], parse_retries=1)
    assert len(trace.llm_messages(1)) == 2, "step 1 was asked again in place"
    rendered = _rendered(composer, trace)
    _check_targets(trace, rendered)
    first, second, third = rendered
    _same_up_to_writing(first, _prompt(trace, 0), rendered)
    _same_up_to_writing(second, _prompt(trace, 1, call=0), rendered)
    recorded = _prompt(trace, 2)
    rejected = next(
        i
        for i, m in enumerate(recorded)
        if m["role"] == "assistant" and m["content"] == UNPARSEABLE
    )
    assert recorded[rejected + 1]["role"] == "user"
    assert str(recorded[rejected + 1]["content"]).startswith("[SYSTEM] Your previous output")
    _same_up_to_writing(third, recorded[:rejected] + recorded[rejected + 2 :], rendered)
    assert len(recorded) == len(third.context) + 2


def _record_browser_use(
    tmp_path: Path, *, cleaner: dict[str, Any], archive: list[dict[str, str]]
) -> Path:
    """A DOM teacher's run on the sticky page: click ``partial``, click ``clear``, done.

    In that order: following ``#clear`` scrolls the page to the anchor, and a
    link scrolled out of the viewport is ``target_offscreen`` (correctly — the
    recorded screenshot does not show it).
    """
    traces = tmp_path / "traces"
    script = [branch_driver.click("partial"), branch_driver.click("clear"), driver.done("checked")]
    with FixtureSite(mapped=True) as site, ScriptedLLMServer(script) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=traces)
        raw["environment"][0]["capabilities"][0]["dom_cleaner"] = cleaner
        raw["hook"][0]["archive"] = archive
        outcome = asyncio.run(
            driver.run_task(raw, task="Open the partial link.", start_url=site.url("sticky"))
        )
        assert llm.errors == []
    outcome.require_finished(3, "checked")
    return traces


def _translations(out: Path) -> dict[int, dict[str, Any]]:
    """Each sample's step → (its translation payload, its target point)."""
    found: dict[int, dict[str, Any]] = {}
    for path in sorted(out.glob("*.samples.jsonl")):
        for line in read_jsonl(path):
            sample = line["sample"]
            if not sample["translation"]:
                continue
            (record,) = sample["translation"]
            (action,) = line["render"]["target_actions"]
            found[sample["step"]] = {
                **record["payload"],
                "target": (action["params"]["coordinate_x"], action["params"]["coordinate_y"]),
                "name": action["name"],
            }
    return found


def _check_partial_below_the_header(partial: dict[str, Any]) -> None:
    x, y = partial["target"]
    assert partial["name"] == "browser.click_coordinate"
    # partial: viewport x 300..540, y 40..140; the sticky header covers y < 100.
    assert 300 <= x < 540 and 100 <= y < 140, partial
    assert partial["occlusion"] == "clear"


def test_browser_use_run_exports_with_dom_raw_occlusion(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    traces = _record_browser_use(
        tmp_path,
        cleaner={"cleaner_type": "browser_use"},
        archive=[{"archiver_id": "browser.dom_raw"}],
    )
    out = tmp_path / "dataset"
    report = run_export(
        composer,
        exporter_entry(
            traces,
            out,
            translator={"translator_id": "index_to_coordinate", "occlusion": "check"},
        ),
    )
    assert sum(report.samples.values()) == 3
    found = _translations(out)
    assert found[0]["evidence"] == "dom_raw" and found[0]["point_source"] == "grid"
    _check_partial_below_the_header(found[0])
    assert found[1]["evidence"] == "dom_raw" and found[1]["point_source"] == "center"
    # The thought is BrowserUse's next_goal; it names no index, so it is kept.
    lines = [line for path in sorted(out.glob("*.samples.jsonl")) for line in read_jsonl(path)]
    assert {
        line["render"]["thought"]["source_field"] for line in lines if line["render"]["thought"]
    } == {"next_goal"}


def test_skyvern_hit_test_run_exports_with_hit_test_occlusion(
    composer: ConfigComposer, tmp_path: Path
) -> None:
    traces = _record_browser_use(
        tmp_path,
        cleaner={"cleaner_type": "skyvern_hit_test", "covered_elements": "drop"},
        archive=[],
    )
    out = tmp_path / "dataset"
    report = run_export(
        composer,
        exporter_entry(
            traces,
            out,
            translator={"translator_id": "index_to_coordinate", "occlusion": "check"},
        ),
    )
    assert sum(report.samples.values()) == 3
    found = _translations(out)
    assert found[0]["evidence"] == "hit_test" and found[0]["point_source"] == "hit_test"
    _check_partial_below_the_header(found[0])
    assert found[1]["evidence"] == "hit_test" and found[1]["point_source"] == "center"
    # No raw DOM was bound: without the hit test the check would have had no evidence.
    assert json.dumps(report.manifest.translator) == json.dumps(
        {
            "occlusion": "check",
            "occlusion_unknown": "raise",
            "translator_id": "index_to_coordinate",
            "untranslatable": "drop_step",
        }
    )
