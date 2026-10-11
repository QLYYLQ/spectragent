"""State branching in a real Chrome, end to end (M31 acceptance; fixture only, no LLM spend).

``tests/_helpers/branch_driver.py`` records one run of ``browser_use``
(``dom_vision``) on the fixture site's ``branch`` pages — every step an index
action: a link, a text input, a radio button, a link, a link — with
``replayability: pinned`` and the browser archive, then branches it at the cart
page (step 4) three times through the real ``trace_branches`` loader and
``BenchmarkRunner``: each branch replays the source's archive offline,
re-executes the recorded prefix with ``prefix_replay`` (``strict``), lets a
scripted teacher decide step 4 and one more step, and ends with a terminal
observation.

- ``b0``'s teacher clicks ``mark`` (a session cookie and a ``localStorage``
  entry, set from JS) and then opens the check page, which renders both;
- ``b1`` and ``b2``'s teachers only open the check page — the same archive,
  seed and teacher output, so they must end on the same screenshot; and
  ``b1`` runs after ``b0`` from the same checkpoint, so it shows whether any
  of ``b0``'s state survived into it.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from spectragent.core.benchmark_runner import TaskOutcome
from spectragent.core.trace.reader import RunTrace
from tests._helpers import branch_driver as branch
from tests._helpers import replay_driver as driver
from tests._helpers.fixture_site import BRANCH_MARK, FixtureSite
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

#: The teachers, in the order the branches run (b1 after b0: the isolation probe).
TEACHERS = {
    "b0": branch.REMEMBER_THEN_CHECK,
    "b1": branch.CHECK,
    "b2": branch.CHECK,
}
#: Restored steps 0..3, the policy step 4 and one continue step, the terminal observation.
ORIGINS = ["restored"] * branch.CHECKPOINT + ["policy", "policy", "terminal"]


@dataclass
class Sweep:
    source: driver.Outcome
    outcomes: dict[str, TaskOutcome]
    traces: dict[str, RunTrace]


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with pytest.MonkeyPatch.context() as patch:
        # The scripted models are on loopback; the machine's proxy must not see them.
        patch.setenv("NO_PROXY", "127.0.0.1,localhost")
        patch.setenv("no_proxy", "127.0.0.1,localhost")
        with FixtureSite(mapped=True) as fixture:
            yield fixture


@pytest.fixture(scope="module")
def sweep(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Sweep:
    base = tmp_path_factory.mktemp("branch")
    with ScriptedLLMServer(branch.RECORDING) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "recorded")
        source = asyncio.run(driver.run_task(raw, task=branch.TASK, start_url=site.url("branch")))
    assert llm.errors == [], llm.errors
    source.require_finished(len(branch.RECORDING), "checked")

    outcomes: dict[str, TaskOutcome] = {}
    traces: dict[str, RunTrace] = {}
    for name, script in TEACHERS.items():
        with ScriptedLLMServer(script) as teacher:
            config = branch.branch_config(
                raw,
                llm=teacher,
                trace_dir=base / "branches",
                sources_root=base / "recorded",
                branches_per_step=len(TEACHERS),
            )
            task_id = f"{source.run_dir.name}/s{branch.CHECKPOINT}/{name}"
            outcomes[name] = branch.run_branch_task(config, task_id)
        assert teacher.errors == [], (name, teacher.errors)
        traces[name] = branch.require_ran(outcomes[name])
    return Sweep(source, outcomes, traces)


def _terminal(trace: RunTrace) -> int:
    (terminal,) = [record.step for record in trace.step_records if record.origin == "terminal"]
    return terminal


# ── 100% prefix verification ─────────────────────────────────────────


def test_prefix_restores_with_every_step_verified(sweep: Sweep) -> None:
    recorded = sweep.source.trace.step_records
    for name, trace in sweep.traces.items():
        records = trace.step_records
        assert [record.origin for record in records] == ORIGINS, name
        for record in records[: branch.CHECKPOINT]:
            check = record.restoration
            assert check is not None and check.verdict == "ok", (name, record.step, check)
            assert record.actions == recorded[record.step].actions, "a copy of the source step"
            # Every recorded action is an element index, re-found by position and signature.
            assert [action.method for action in check.actions] == ["geometry_signature"]
            (restored,) = check.actions
            assert restored.executed is not None
            assert restored.executed.name == restored.recorded.name
            assert restored.live_success is True and restored.recorded_success is True
        checkpoint = records[branch.CHECKPOINT].restoration
        assert checkpoint is not None and checkpoint.verdict == "ok"
        assert checkpoint.step == branch.CHECKPOINT and checkpoint.actions == []
        for record in records[: branch.CHECKPOINT + 1]:
            assert record.restoration is not None
            (evidence,) = record.restoration.evidence
            assert evidence.capability_id == "browser"
            assert evidence.url_match is True and evidence.signature_match is True
            assert evidence.screenshot_distance is not None and evidence.screenshot_distance <= 5
            assert evidence.document_misses == 0, "the archive served every main-frame document"
        result = sweep.outcomes[name].result
        assert result is not None and result.metrics.steps == 2, "only the teacher's steps count"
    assert branch.restoration_rate(trace.trace_dir for trace in sweep.traces.values()).rate == 1.0


def test_each_branch_is_a_task_of_the_sweep(sweep: Sweep) -> None:
    source = sweep.source.run_dir
    for index, (name, trace) in enumerate(sweep.traces.items()):
        config = trace.config
        assert config.benchmark == "trace_branches" and config.loop_id == "branch"
        assert config.task_id == f"{source.name}/s{branch.CHECKPOINT}/{name}"
        assert config.branch is not None
        assert config.branch.source_run_id == source.name
        assert config.branch.source_trace == source.name
        assert (config.branch.checkpoint_step, config.branch.branch_index) == (
            branch.CHECKPOINT,
            index,
        )
        assert config.branch.branch_group_id == f"{source.name}/s{branch.CHECKPOINT}"
        assert config.task_setup is not None
        assert config.task_setup.context["replay_archive"] == str(source.resolve())
        assert config.task == branch.TASK


# ── determinism and isolation ────────────────────────────────────────


def test_two_branches_end_on_the_same_screenshot(sweep: Sweep) -> None:
    """Same archive, same seed, same teacher output: the same last frame, byte for byte."""
    b1, b2 = sweep.traces["b1"], sweep.traces["b2"]
    shots = [trace.screenshot(_terminal(trace)) for trace in (b1, b2)]
    assert all(shots), "the terminal observation has a screenshot"
    assert (
        hashlib.sha256(shots[0] or b"").hexdigest() == hashlib.sha256(shots[1] or b"").hexdigest()
    )
    assert driver.step_actions(b1) == driver.step_actions(b2)


def test_branches_do_not_share_state(sweep: Sweep) -> None:
    b0, b1 = sweep.traces["b0"], sweep.traces["b1"]
    remembered = branch.page_text(b0, _terminal(b0))
    assert f"cookie: {BRANCH_MARK}=remembered" in remembered
    assert "storage: remembered" in remembered
    assert BRANCH_MARK in branch.storage_names(b0), "browser.storage recorded b0's state"

    # b1 ran after b0, from the same checkpoint, in a browser of its own.
    clean = branch.page_text(b1, _terminal(b1))
    assert "cookie: (none)" in clean and "storage: (none)" in clean
    assert BRANCH_MARK not in branch.storage_names(b1)
    assert all(
        record.restoration.verdict == "ok"
        for record in b1.step_records
        if record.restoration is not None
    ), "b1 verifies on its own"
    # And the source never saw the mark either.
    assert "cookie: (none)" in branch.page_text(sweep.source.trace, branch.CHECKPOINT + 1)


# ── PRD §9: the restoration verification rate ────────────────────────


def test_restoration_rate_is_reported(sweep: Sweep, capsys: pytest.CaptureFixture[str]) -> None:
    dirs = [outcome.trace_dir for outcome in sweep.outcomes.values()]
    assert all(isinstance(path, Path) for path in dirs)
    rate = branch.restoration_rate(path for path in dirs if path is not None)
    # Every restored step (0..3) and every checkpoint step (4), in each branch.
    assert rate.total == (branch.CHECKPOINT + 1) * len(TEACHERS)
    assert (rate.ok, dict(rate.by_reason)) == (rate.total, {})
    ran = branch.components(sweep.traces["b0"])
    assert {key: ran[key] for key in ran if key != "route"} == {
        "agent": "browser_use",
        "loop": "branch",
        "restorer": "prefix_replay",
        "cleaner": "browser_use",
        "serializer": "text",
        "backend": "local_chrome",
        "network_source": "archive",
    }
    with capsys.disabled():
        print(f"\n[M31] restoration verification rate: {rate.summary()}; ran: {ran}")
