"""An AndroidWorld information-retrieval task, run end to end with a scripted model, scored on the answer.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
the same ``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). ``task_list.py`` writes the list for one
information-retrieval task under AndroidWorld's interpreter, and the
``androidworld`` loader reads it. Each test is one run through
``AgentRuntime.execute``: the Android capability, composed from YAML with
``task_oracle: androidworld``, boots its own read-only emulator and
AndroidWorld's service prepares the task; UI-TARS reads its first reply,
``finished(content=...)``, from a scripted model, so the run ends at once with
the Loop's ``loop.done`` and that answer; at the scoring point the oracle hands
the answer to AndroidWorld before it asks for the score
(``docs/architecture_design.md`` §4.2, §11.2). The answer AndroidWorld expects
is worked out under its own interpreter, for the same instance:

- the expected answer scores 1, and the score says the answer was sent while
  ``oracle_scores.json`` holds no answer text; ``result.json`` holds it;
- a wrong answer scores 0;
- the expected answer with a stray double quote scores 1 too, as AndroidWorld's
  own runner scores it: the launcher keeps answers off the device's screen,
  whose shell failed on the quote (HTTP 500, the run not scored).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue

from spectragent import PACKAGE_ROOT
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.oracle.androidworld import Installation
from spectragent.core.composer import ConfigComposer
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace import RunTrace
from spectragent.schemas.task import TaskContext, TaskDefinition
from tests._helpers.assembly import AGENT_ID, make_run

ANDROIDWORLD_VARIABLE = "SPECTRAGENT_TEST_ANDROIDWORLD"
APK_VARIABLE = "SPECTRAGENT_TEST_SERVICE_APK"
#: Scored on the title of the calendar event at one time of one day. AndroidWorld
#: compares titles loosely, and this one is long enough that one stray
#: character does not change the score.
TASK = "SimpleCalendarEventOnDateAtTime"
TASK_ID = f"{TASK}/42/0"
TASK_LIST_WRITER = (
    PACKAGE_ROOT / "core/capabilities/android/oracle/androidworld_service/task_list.py"
)
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


def _installation_problem() -> str | None:
    root = os.environ.get(ANDROIDWORLD_VARIABLE)
    if not root:
        return f"set {ANDROIDWORLD_VARIABLE} to an AndroidWorld installation"
    if not os.environ.get(APK_VARIABLE):
        return f"set {APK_VARIABLE} to the device-side APK"
    problems = Installation(Path(root).expanduser()).problems()
    return "; ".join(problems) or None


pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(_installation_problem() is not None, reason=str(_installation_problem())),
]


class _ScriptedModel:
    """Answers each call with the next reply of its script; no model is called."""

    inference_kind = "chat"
    model = name = "scripted"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)

    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: object = None
    ) -> ChatInvokeCompletion:
        if not self.replies:
            raise AssertionError("the script has no reply left")
        return ChatInvokeCompletion(completion=self.replies.pop(0), usage=None)

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


@dataclass(frozen=True)
class Listed:
    """The listed task, its setup context, and the answer AndroidWorld expects of it."""

    task: TaskDefinition
    context: TaskContext
    expected: str


@pytest.fixture(scope="module")
def listed(tmp_path_factory: pytest.TempPathFactory) -> Listed:
    installation = Installation(Path(os.environ[ANDROIDWORLD_VARIABLE]).expanduser())
    data = tmp_path_factory.mktemp("androidworld")
    bare = {"PATH": "/usr/bin:/bin", "HOME": str(data)}
    subprocess.run(
        [str(installation.python), "-I", str(TASK_LIST_WRITER), "--src", str(installation.src)]
        + ["--out", str(data), "--tasks", TASK],
        check=True,
        capture_output=True,
        timeout=120,
        env=bare,
    )
    expected = subprocess.run(
        [str(installation.python), "-I", "-c", EXPECTED_ANSWER, str(installation.src), TASK],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env=bare,
    ).stdout.strip()
    loader = AndroidWorldTaskLoader(data_dir=str(data))
    assert expected and loader.skipped == {}, "an information-retrieval task the loader keeps"
    return Listed(task=loader[TASK_ID], context=loader.get_task_context(TASK_ID), expected=expected)


async def _run(listed: Listed, answer: str, traces: Path) -> RunTrace:
    """One run whose agent ends at once with *answer*; its trace."""
    raw: dict[str, JsonValue] = {
        "capability": [
            {
                "capability_id": "android",
                "backend": {
                    "provider_id": "local_emulator",
                    "avd": os.environ["SPECTRAGENT_TEST_AVD"],
                },
                "service_apk": os.environ[APK_VARIABLE],
                "tap_injector": {"injector_id": "shell_injection"},
                "typing_backend": {"typing_id": "set_text"},
                "task_oracle": {
                    "oracle_id": "androidworld",
                    "root": os.environ[ANDROIDWORLD_VARIABLE],
                },
            }
        ]
    }
    capability = ConfigComposer.for_config(raw).compose(raw).single("capability")
    assert isinstance(capability, AndroidCapability)
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    writer = TraceWriter(trace_dir=str(traces))
    run = make_run(
        agent=UITarsAgent(prompt_template="mobile_use"),
        loop=ReactLoop(max_steps=3),
        env=env,
        llm=_ScriptedModel(f"Action: finished(content={answer!r})"),
        memory=SimpleMemory(),
        hooks=[writer],
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

    (score,) = trace.oracle_scores()
    assert (score.status, score.score) == ("scored", 1.0), score.detail
    assert (score.provenance["task"], score.provenance["answer"]) == (TASK, "sent")
    assert trace.result is not None and trace.result.answer == listed.expected
    assert listed.expected not in (trace.trace_dir / "oracle_scores.json").read_text()
    print(f"{TASK}: answer {listed.expected!r} scored {score.score} in {score.duration_ms} ms")


async def test_a_wrong_answer_scores_0(listed: Listed, tmp_path: Path) -> None:
    trace = await _run(listed, "Something else entirely", tmp_path)

    (score,) = trace.oracle_scores()
    assert (score.status, score.score) == ("scored", 0.0), score.detail
    assert score.provenance["answer"] == "sent"
    print(f"{TASK}: a wrong answer scored {score.score} in {score.duration_ms} ms")


async def test_an_answer_with_a_stray_double_quote_is_scored_as_androidworld_scores_it(
    listed: Listed, tmp_path: Path
) -> None:
    answer = listed.expected + '"'
    trace = await _run(listed, answer, tmp_path)

    (score,) = trace.oracle_scores()
    assert (score.status, score.score) == ("scored", 1.0), score.detail
    print(f"{TASK}: answer {answer!r} scored {score.score} in {score.duration_ms} ms")
