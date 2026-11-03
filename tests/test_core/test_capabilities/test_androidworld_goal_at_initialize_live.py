"""AndroidWorld tasks whose goal is made or reworded at initialize, run end to end on a real emulator.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
the same ``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). Each test is one run through
``AgentRuntime.execute`` on a task ``task_list.py`` lists under AndroidWorld's
interpreter: the Android capability, composed from YAML with ``task_oracle:
androidworld``, boots its own read-only emulator, and AndroidWorld's service
prepares the task. The goal the device holds after initialize completes the
run's task text (``docs/architecture_design.md`` §4.10, §11.2):

- ``click-button`` (MiniWoB++): the page makes its instruction at initialize.
  M3A runs on a stand-in model that reads the goal and the element list off
  its prompt and clicks the element the goal quotes, so the goal must reach
  the prompt. The page then reports its reward, the environment ends the
  episode (``episode_over``) and the run scores 1. The episode question,
  asked once the screen is still, is timed from the click: whether the page
  had stored its reward by then, and if not, how long until it had;
- ``SimpleCalendarAnyEventsOnDate`` (information retrieval): initialize rewords
  the date its goal names, with the instance's seed. UI-TARS answers at once
  with the expected answer; the run's text is the reworded goal, the one
  AndroidWorld's own code draws for the instance offline, and the run scores 1.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from spectragent.agents.m3a import M3AAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.oracle.androidworld import (
    AndroidWorldError,
    AndroidWorldOracle,
)
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace import RunTrace
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.android_runs import (
    M3A_GOAL_LINE,
    MINIWOB_GUIDELINE,
    ScriptedModel,
    androidworld_capability,
    installation,
    installation_problem,
    prompt_text,
    write_task_list,
)
from tests._helpers.assembly import AGENT_ID, make_run

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
]

#: The fixed part a MiniWoB++ goal starts with, which the task list records.
MINIWOB_PREFIX = "Follow the instructions shown on the top of the screen"
#: The answer AndroidWorld expects of an information-retrieval instance and the goal its
#: initialize rewords it to, worked out offline as initialize does: ``random`` seeded
#: with the instance's seed, the task's proto filled in, then the dates reworded.
DRAWN_OFFLINE = """
import json, random, sys
sys.path.insert(0, sys.argv[1])
from android_world import registry, suite_utils
from android_world.task_evals.information_retrieval import information_retrieval, proto_utils
name = sys.argv[2]
family = registry.TaskRegistry().get_registry("information_retrieval")
task = suite_utils.create_suite(family, n_task_combinations=1, seed=42, tasks=[name])[name][0]
random.seed(task.params["seed"])
proto_utils.initialize_proto(task.task, task.params)
answer = ", ".join(str(value) for value in proto_utils.get_expected_answer(task.task))
information_retrieval._maybe_replace_date(task.params)
print(json.dumps({"answer": answer, "goal": task.goal}))
"""


def _all_text(messages: Sequence[BaseMessage]) -> str:
    """Every message's text: UI-TARS says the task in its seed, the system message."""
    texts: list[str] = []
    for message in messages:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            texts.append(content)
        else:
            texts += [part.text for part in content if getattr(part, "type", None) == "text"]
    return "\n".join(texts)


class ClickWhatTheGoalQuotes:
    """Stands in for M3A's model: clicks the element whose text the goal quotes.

    A MiniWoB++ page makes its instruction when the task starts, so no script
    written beforehand can answer it; this reads the goal and the element list
    off the action-selection prompt instead. A summary call (the history line
    of the step before) gets a fixed line. Keeps every call's messages.
    """

    inference_kind = "chat"
    model = name = "goal-reader"

    def __init__(self) -> None:
        self.calls: list[list[BaseMessage]] = []
        self.goals: list[str] = []

    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: object = None
    ) -> ChatInvokeCompletion[str]:
        self.calls.append(list(messages))
        text = prompt_text(messages)
        if M3A_GOAL_LINE not in text:
            return ChatInvokeCompletion(completion="The page changed after the click.", usage=None)
        goal = text.split(M3A_GOAL_LINE, 1)[1].split("\n", 1)[0]
        self.goals.append(goal)
        quoted = re.search(r'"([^"]+)"', goal.removeprefix(MINIWOB_PREFIX))
        if quoted is not None:
            for line in text.splitlines():
                element = re.match(r"UI element (\d+): (\{.*\})$", line)
                if element and f'"text": "{quoted.group(1)}"' in element.group(2):
                    return ChatInvokeCompletion(
                        completion=(
                            f'Reason: The goal asks for the "{quoted.group(1)}" button.\n'
                            f'Action: {{"action_type": "click", "index": {element.group(1)}}}'
                        ),
                        usage=None,
                    )
        return ChatInvokeCompletion(
            completion=(
                f"Reason: No element the goal {goal!r} quotes.\n"
                'Action: {"action_type": "status", "goal_status": "infeasible"}'
            ),
            usage=None,
        )

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


@dataclass
class EpisodeTimes:
    """Each episode question, timed from the moment the step's last action returned."""

    #: (seconds from the action to the question, the answer, seconds the question took)
    asked: list[tuple[float, bool, float]] = field(default_factory=list)
    #: After the first no: seconds from the action until a probe first heard yes, if it did.
    over_after_s: float | None = None
    probed: bool = False


def _timed(oracle: AndroidWorldOracle, times: EpisodeTimes, acted: list[float]) -> None:
    """Time the oracle's episode question; after the first no, probe until the page stores its reward.

    The Loop still gets each first answer, so the run goes as it would. The
    probe asks for up to 10 s after the action and stops at its first error.
    """
    real = oracle.episode_over

    async def episode_over() -> bool:
        asked = time.monotonic()
        over = await real()
        times.asked.append((asked - acted[-1], over, time.monotonic() - asked))
        if not over and not times.probed:
            times.probed = True
            while time.monotonic() - acted[-1] < 10:
                await asyncio.sleep(0.1)
                try:
                    heard = await real()
                except AndroidWorldError:
                    break
                if heard:
                    times.over_after_s = time.monotonic() - acted[-1]
                    break
        return over

    oracle.episode_over = episode_over  # type: ignore[method-assign]


@dataclass(frozen=True)
class Ran:
    trace: RunTrace
    model_goals: list[str]
    times: EpisodeTimes


async def _run_click_button(data: Path, traces: Path) -> Ran:
    """``click-button`` through M3A on the goal-reading model; the trace and the episode times."""
    task_id = "click-button/42/0"
    loader = AndroidWorldTaskLoader(data_dir=str(data))
    context = loader.get_task_context(task_id)
    capability = androidworld_capability()
    oracle = capability.task_oracle
    assert isinstance(oracle, AndroidWorldOracle)
    times, acted = EpisodeTimes(), [time.monotonic()]
    _timed(oracle, times, acted)
    execute_action = capability.execute_action

    async def act_and_note(action: Action) -> ActionResult:
        try:
            return await execute_action(action)
        finally:
            acted.append(time.monotonic())

    capability.execute_action = act_and_note  # type: ignore[method-assign]
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    writer = TraceWriter(trace_dir=str(traces))
    model = ClickWhatTheGoalQuotes()
    run = make_run(
        agent=M3AAgent(task_guidelines=(MINIWOB_GUIDELINE,)),
        loop=ReactLoop(
            max_steps=120,
            max_actions_per_step=2,
            parse_retries_per_step=0,
            max_consecutive_parse_errors=None,
        ),
        env=env,
        llm=model,
        memory=SlidingWindowMemory(window_size=1),
        hooks=[writer],
    )
    result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
        loader[task_id].task_text,
        task_id=task_id,
        benchmark="androidworld",
        subset="miniwob",
        env_context=context.env,
        loop_inputs=context.loop,
        timeout=300,
    )
    assert result.agent_done == "episode_over", (result.agent_done, result.error, model.goals)
    assert writer.run_dir is not None
    return Ran(trace=RunTrace(writer.run_dir), model_goals=model.goals, times=times)


async def test_a_miniwob_goal_made_on_the_page_reaches_the_agent_and_the_page_ends_the_episode(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    write_task_list(data, "click-button", family="miniwob")
    ran = await _run_click_button(data, tmp_path / "traces")
    trace = ran.trace

    made = trace.setup_task_text()
    assert made is not None and made.capability_id == "android"
    assert made.text.startswith(f"{MINIWOB_PREFIX}: ") and len(made.text) > len(MINIWOB_PREFIX) + 2
    assert (trace.config.task, trace.config.task_text_setter) == (f"{MINIWOB_PREFIX}.", "android")
    assert trace.task_text() == made.text
    assert ran.model_goals and set(ran.model_goals) == {made.text}, "every prompt had the goal"
    assert trace.result is not None and trace.result.task == made.text

    (score,) = trace.oracle_scores()
    assert (score.status, score.score) == ("scored", 1.0), score.detail
    assert score.provenance["goal"] == made.text
    policy = [record for record in trace.step_records if record.origin == "policy"]
    over = [answer for _, answer, _ in ran.times.asked]
    assert over[-1] is True and len(over) == len(policy)
    for step, (after_s, answer, took_s) in enumerate(ran.times.asked):
        print(
            f"click-button step {step}: asked {after_s * 1000:.0f} ms after the action, "
            f"over={answer}, the question took {took_s * 1000:.0f} ms"
        )
    if ran.times.over_after_s is not None:
        print(f"the page had stored its reward {ran.times.over_after_s * 1000:.0f} ms after it")
    print(f"goal {made.text!r}; {len(policy)} step(s); score {score.score}")


async def test_a_goal_whose_date_initialize_rewords_is_the_runs_text(tmp_path: Path) -> None:
    task, task_id = "SimpleCalendarAnyEventsOnDate", "SimpleCalendarAnyEventsOnDate/42/0"
    aw = installation()
    data = tmp_path / "data"
    write_task_list(data, task)
    drawn = json.loads(
        subprocess.run(
            [str(aw.python), "-I", "-c", DRAWN_OFFLINE, str(aw.src), task],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": "/usr/bin:/bin", "HOME": str(data)},
        ).stdout
    )
    loader = AndroidWorldTaskLoader(data_dir=str(data))
    listed = loader[task_id]
    assert listed.task_text != drawn["goal"], "this instance's date is reworded"

    capability = androidworld_capability()
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
    model = ScriptedModel(f"Action: finished(content={drawn['answer']!r})")
    run = make_run(
        agent=UITarsAgent(prompt_template="mobile_use"),
        loop=ReactLoop(max_steps=3),
        env=env,
        llm=model,
        memory=SimpleMemory(),
        hooks=[writer],
    )
    result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
        listed.task_text,
        task_id=task_id,
        benchmark="androidworld",
        env_context=loader.get_task_context(task_id).env,
        timeout=180,
    )
    assert result.agent_done == "done", result.error
    assert writer.run_dir is not None
    trace = RunTrace(writer.run_dir)

    assert (trace.config.task, trace.config.task_text_setter) == (listed.task_text, "android")
    assert trace.task_text() == result.task == drawn["goal"], "the goal AndroidWorld draws offline"
    (call,) = model.calls
    said = _all_text(call)
    assert drawn["goal"] in said and listed.task_text not in said, "seeded with the reworded goal"
    (score,) = trace.oracle_scores()
    assert (score.status, score.score) == ("scored", 1.0), score.detail
    print(f"{task}: listed {listed.task_text!r}; ran on {trace.task_text()!r}; score {score.score}")
