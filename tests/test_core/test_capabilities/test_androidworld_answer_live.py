"""An AndroidWorld information-retrieval task, run end to end with a scripted model, scored on the answer.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
the same ``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). ``task_list.py`` writes the list for one
information-retrieval task under AndroidWorld's interpreter, and the
``androidworld`` loader reads it. Each test is one run through
``AgentRuntime.execute``: the Android capability, composed from YAML and
hosting the ``androidworld`` benchmark's setup component, boots its own
read-only emulator and AndroidWorld's service prepares the task; UI-TARS reads
its first reply, ``finished(content=...)``, from a scripted model, so the run
ends at once with the Loop's ``loop.done`` and that answer; at the scoring
point the answer goes to AndroidWorld before the score is asked for
(``docs/architecture_design.md`` §4.2, §11.2). The answer AndroidWorld expects
is worked out under its own interpreter, for the same instance:

- the expected answer scores 1, and the score says the answer was sent while
  the verdict holds no answer text; ``result.json`` holds it;
- a wrong answer scores 0;
- the expected answer with a stray double quote scores 1 too, as AndroidWorld's
  own runner scores it: the launcher keeps answers off the device's screen,
  whose shell failed on the quote (HTTP 500, the run not judged).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace import RunTrace
from spectragent.schemas.task import TaskContext, TaskDefinition
from tests._helpers.android_runs import (
    ScriptedModel,
    androidworld_benchmark,
    androidworld_capability,
    installation,
    installation_problem,
    write_task_list,
)
from tests._helpers.assembly import AGENT_ID, make_run

#: Scored on the title of the calendar event at one time of one day. AndroidWorld
#: compares titles loosely, and this one is long enough that one stray
#: character does not change the score.
TASK = "SimpleCalendarEventOnDateAtTime"
TASK_ID = f"{TASK}/42/0"
#: Prints the answer AndroidWorld expects of the instance ``task_list.py`` lists
#: (suite seed 42, index 0), as its task makes it at initialize: ``random``
#: seeded with the instance's seed, then the task's proto filled in.
EXPECTED_ANSWER = """
import random, sys
sys.path.insert(0, sys.argv[1])
from android_world import registry, suite_utils
from android_world.task_evals.information_retrieval import proto_utils
name = sys.argv[2]
family = registry.TaskRegistry().get_registry("information_retrieval")
task = suite_utils.create_suite(family, n_task_combinations=1, seed=42, tasks=[name])[name][0]
random.seed(task.params["seed"])
proto_utils.initialize_proto(task.task, task.params)
print(", ".join(str(value) for value in proto_utils.get_expected_answer(task.task)))
"""

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
]


@dataclass(frozen=True)
class Listed:
    """The listed task, its setup context, its loader, and the answer AndroidWorld expects of it."""

    task: TaskDefinition
    context: TaskContext
    loader: AndroidWorldTaskLoader
    expected: str


@pytest.fixture(scope="module")
def listed(tmp_path_factory: pytest.TempPathFactory) -> Listed:
    aw = installation()
    data = tmp_path_factory.mktemp("androidworld")
    write_task_list(data, TASK)
    expected = subprocess.run(
        [str(aw.python), "-I", "-c", EXPECTED_ANSWER, str(aw.src), TASK],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(data)},
    ).stdout.strip()
    loader = AndroidWorldTaskLoader(data_dir=str(data))
    assert expected and loader.skipped == {}, "an information-retrieval task the loader keeps"
    return Listed(
        task=loader[TASK_ID],
        context=loader.get_task_context(TASK_ID),
        loader=loader,
        expected=expected,
    )


async def _run(listed: Listed, answer: str, traces: Path) -> RunTrace:
    """One run whose agent ends at once with *answer*; its trace."""
    capability = androidworld_capability()
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    writer = TraceWriter(trace_dir=str(traces))
    run = make_run(
        agent=UITarsAgent(prompt_template="mobile_use"),
        loop=ReactLoop(max_steps=3),
        env=env,
        llm=ScriptedModel(f"Action: finished(content={answer!r})"),
        memory=SimpleMemory(),
        hooks=[writer],
        benchmark=androidworld_benchmark(listed.loader),
    )
    result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
        listed.task.task_text,
        task_id=TASK_ID,
        benchmark="androidworld",
        env_context=listed.context.env,
        timeout=120,
    )
    assert (result.agent_done, result.answer) == ("done", answer), result.error
    assert writer.run_dir is not None
    return RunTrace(writer.run_dir)


async def test_the_expected_answer_scores_1_and_its_text_stays_in_the_result(
    listed: Listed, tmp_path: Path
) -> None:
    trace = await _run(listed, listed.expected, tmp_path)

    (score,) = trace.verdicts().values()
    assert (score.status, score.score) == ("judged", 1.0), score.detail
    assert (score.evidence["task"], score.evidence["answer"]) == (TASK, "sent")
    assert trace.result is not None and trace.result.answer == listed.expected
    assert listed.expected not in (trace.trace_dir / "verdicts" / f"{score.slug}.json").read_text()
    print(f"{TASK}: answer {listed.expected!r} scored {score.score} in {score.duration_ms} ms")


async def test_a_wrong_answer_scores_0(listed: Listed, tmp_path: Path) -> None:
    trace = await _run(listed, "Something else entirely", tmp_path)

    (score,) = trace.verdicts().values()
    assert (score.status, score.score) == ("judged", 0.0), score.detail
    assert score.evidence["answer"] == "sent"
    print(f"{TASK}: a wrong answer scored {score.score} in {score.duration_ms} ms")


async def test_an_answer_with_a_stray_double_quote_is_scored_as_androidworld_scores_it(
    listed: Listed, tmp_path: Path
) -> None:
    answer = listed.expected + '"'
    trace = await _run(listed, answer, tmp_path)

    (score,) = trace.verdicts().values()
    assert (score.status, score.score) == ("judged", 1.0), score.detail
    print(f"{TASK}: answer {answer!r} scored {score.score} in {score.duration_ms} ms")
