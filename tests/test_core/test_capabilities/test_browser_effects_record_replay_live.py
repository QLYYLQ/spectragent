"""Action effects and capture state through record, same-path replay and branch (no LLM spend).

``browser_use`` (``dom_vision``) with a scripted model records the fixture
site's ``branch`` flow with a text entry, a key press and a scroll added —
every effect kind a vision-and-DOM run produces — under ``replayability:
pinned`` and the browser archive. Then:

- **same-path replay** of that recording, served from its archive: every
  recorded effect and ``capture_state`` reads back through ``RunTrace`` as its
  own type and re-serializes to the bytes the step file holds, and the
  replay's measured effects describe the same facts the recording's did
  (``docs/architecture_design.md`` §4.5, "动作效果"; §4.4, "跨平台采集状态");
- **branch, history from the source**: ``BranchLoop`` rebuilds every prefix
  prompt from the recorded observations and records and refuses any byte of
  difference from the recorded prompt, so a branch that runs proves the
  effect lines and capture-state text read back identically;
- **branch, restored history**: the prefix is re-executed and the policy
  step's prompt renders the effects actually measured during restoration.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.benchmark_runner import TaskOutcome
from spectragent.core.capabilities.browser.effects import (
    PointerEffect,
    ScrollEffect,
    TextInputEffect,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.observation.capture_state import CaptureStateFragment
from tests._helpers import branch_driver as branch
from tests._helpers import replay_driver as driver
from tests._helpers.fixture_site import FixtureSite
from tests._helpers.scripted_llm import Reply, ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

TASK = "Order a large blue kettle and check the saved state."

#: The recorded flow, one action a step: link, text entry, a key, a scroll, a
#: radio button, the cart link, the check link, done.
RECORDING: list[Reply] = [
    branch.click("open-form"),
    branch.type_into("item", branch.ITEM),
    driver.reply({"browser.send_keys": {"keys": "End"}}, goal="move to the end"),
    driver.reply({"browser.scroll": {"direction": "down"}}, goal="look below"),
    branch.click("size-large"),
    branch.click("add-to-cart"),
    branch.click("check"),
    driver.done("checked"),
]
#: The step the branches fork at: the cart page, before the check link.
CHECKPOINT = 6
#: What each recorded step's action reports.
EFFECTS = [
    PointerEffect,
    TextInputEffect,
    TextInputEffect,
    ScrollEffect,
    PointerEffect,
    PointerEffect,
    PointerEffect,
]
#: The teacher at the checkpoint: open the check page, finish.
TEACHER: list[Reply] = [branch.click("check"), driver.done("checked")]


@dataclass
class Runs:
    source: RunTrace
    replay: RunTrace
    branch_source: RunTrace
    branch_restored: RunTrace
    source_prompts: list[str]
    restored_prompts: list[str]


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NO_PROXY", "127.0.0.1,localhost")
        patch.setenv("no_proxy", "127.0.0.1,localhost")
        with FixtureSite(mapped=True) as fixture:
            yield fixture


def _prompt_texts(llm: ScriptedLLMServer) -> list[str]:
    texts: list[str] = []
    for body in llm.requests:
        parts: list[str] = []
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            else:
                parts.extend(
                    part.get("text", "") for part in content or [] if part.get("type") == "text"
                )
        texts.append("\n".join(parts))
    return texts


def _branch(
    raw: dict[str, Any], base: Path, source: driver.Outcome, *, history: str, name: str
) -> tuple[TaskOutcome, list[str]]:
    with ScriptedLLMServer(TEACHER) as teacher:
        config = branch.branch_config(
            raw,
            llm=teacher,
            trace_dir=base / name,
            sources_root=base / "recorded",
            step=CHECKPOINT,
        )
        config["agent"][0]["agent_loop"]["history_source"] = history
        outcome = branch.run_branch_task(config, f"{source.run_dir.name}/s{CHECKPOINT}/b0")
    assert teacher.errors == [], teacher.errors
    return outcome, _prompt_texts(teacher)


@pytest.fixture(scope="module")
def runs(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Runs:
    base = tmp_path_factory.mktemp("effects")
    with ScriptedLLMServer(RECORDING) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "recorded")
        source = asyncio.run(driver.run_task(raw, task=TASK, start_url=site.url("branch")))
        source_prompts = _prompt_texts(llm)
    assert llm.errors == [], llm.errors
    source.require_finished(len(RECORDING), "checked")

    with ScriptedLLMServer(RECORDING) as llm:
        replay_raw = driver.to_replay_config(raw, llm=llm, trace_dir=base / "replayed")
        replay = asyncio.run(
            driver.run_task(
                replay_raw,
                task=TASK,
                start_url=site.url("branch"),
                replay_archive=source.run_dir,
            )
        )
    assert llm.errors == [], llm.errors
    replay.require_finished(len(RECORDING), "checked")

    by_source, _ = _branch(raw, base, source, history="source", name="branch-source")
    by_restored, restored_prompts = _branch(
        raw, base, source, history="restored", name="branch-restored"
    )
    return Runs(
        source=source.trace,
        replay=replay.trace,
        branch_source=branch.require_ran(by_source),
        branch_restored=branch.require_ran(by_restored),
        source_prompts=source_prompts,
        restored_prompts=restored_prompts,
    )


def _effects(trace: RunTrace) -> list[Any]:
    return [
        result.effect
        for record in trace.step_records
        if record.origin != "terminal"
        for result in record.action_results
    ]


def _capture_states(trace: RunTrace) -> list[Any]:
    return [
        trace.observation_fragment(step, "browser.capture_state")
        for step in range(trace.step_count)
    ]


def test_the_recording_measured_every_effect_and_reads_it_back_typed(runs: Runs) -> None:
    effects = _effects(runs.source)
    assert [type(effect) for effect in effects[: len(EFFECTS)]] == EFFECTS
    assert effects[len(EFFECTS)] is None  # done reports no effect
    typed, pressed, scrolled = effects[1], effects[2], effects[3]
    assert isinstance(typed, TextInputEffect) and typed.value_changed is True
    assert isinstance(pressed, TextInputEffect) and pressed.keys == ("End",)
    assert pressed.value_changed is False
    assert isinstance(scrolled, ScrollEffect) and scrolled.container == "page"
    for record in runs.source.step_records:
        written = json.loads(
            (runs.source.trace_dir / "steps" / f"{record.step:03d}.json").read_text()
        )
        assert record.model_dump(mode="json") == written


def test_every_observation_carries_capture_state_that_reads_back(runs: Runs) -> None:
    states = _capture_states(runs.source)
    assert all(isinstance(state, CaptureStateFragment) for state in states)
    assert states[0].pointer.status == "unknown"  # no input yet
    assert states[1].pointer.status == "known"  # the click on the link
    assert all(state.failed_visible_resources.count == 0 for state in states)
    assert all(state.settle_state is not None for state in states)


def test_the_model_was_shown_effect_lines_and_capture_state(runs: Runs) -> None:
    effects = _effects(runs.source)
    for step, prompt in enumerate(runs.source_prompts[1 : len(EFFECTS) + 1], start=1):
        assert effects[step - 1].describe() in prompt, step
        assert "<capture_state>" in prompt


def test_the_replay_reads_back_and_measures_the_same_facts(runs: Runs) -> None:
    source, replay = _effects(runs.source), _effects(runs.replay)
    assert [type(e) for e in replay] == [type(e) for e in source]
    for recorded, replayed in zip(source, replay, strict=True):
        if isinstance(recorded, PointerEffect):
            assert isinstance(replayed, PointerEffect)
            assert (replayed.hit, replayed.on_target) == (recorded.hit, recorded.on_target)
        elif isinstance(recorded, TextInputEffect):
            assert isinstance(replayed, TextInputEffect)
            assert (replayed.target, replayed.value_changed, replayed.keys) == (
                recorded.target,
                recorded.value_changed,
                recorded.keys,
            )
        elif isinstance(recorded, ScrollEffect):
            assert isinstance(replayed, ScrollEffect)
            assert (replayed.container, replayed.delta, replayed.at_boundary) == (
                recorded.container,
                recorded.delta,
                recorded.at_boundary,
            )
    assert all(isinstance(state, CaptureStateFragment) for state in _capture_states(runs.replay))


def test_a_source_history_branch_rebuilds_the_recorded_prompts_exactly(runs: Runs) -> None:
    """``BranchLoop`` refuses a prefix whose rebuilt prompt differs by a byte from the recording."""
    records = runs.branch_source.step_records
    assert [record.origin for record in records[:CHECKPOINT]] == ["restored"] * CHECKPOINT
    restored = [
        result.effect for record in records[:CHECKPOINT] for result in record.action_results
    ]
    assert [type(effect) for effect in restored] == EFFECTS[:CHECKPOINT]


def test_a_restored_history_branch_shows_the_effects_restoration_measured(runs: Runs) -> None:
    records = runs.branch_restored.step_records
    restored = [record for record in records if record.origin == "restored"]
    assert len(restored) == CHECKPOINT
    last = restored[-1]
    assert last.action_results and last.action_results[0].effect is not None
    policy_prompt = runs.restored_prompts[0]
    assert last.action_results[0].effect.describe() in policy_prompt
