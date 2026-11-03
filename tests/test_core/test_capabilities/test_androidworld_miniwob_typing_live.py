"""MiniWoB++'s ``terminal``, typed with key events, end to end on a real emulator.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
the same ``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). The page reads ``keydown`` events on a
hidden input, so only key events type into it. Neither of the service's two
typing backends sends one, and this task is why AndroidWorld and MiniWoB++
runs type with ``key_events`` (user ruling 2026-11-03,
``docs/architecture_design.md`` §11.2 "策略轴"). M3A runs on a stand-in model
that types ``ls``, reads the listing off its prompt's element list, then types
``rm`` with the file the goal asks for; M3A presses Enter after each. The page
ends the episode and the run scores 1.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from spectragent.agents.m3a import M3AAgent
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.core.capabilities.android.effects import AndroidTextEffect
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace import RunTrace
from tests._helpers.android_runs import (
    M3A_GOAL_LINE,
    MINIWOB_GUIDELINE,
    androidworld_capability,
    installation_problem,
    prompt_text,
    write_task_list,
)
from tests._helpers.assembly import AGENT_ID, make_run

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
]

_ELEMENT = re.compile(r"^UI element (\d+): (\{.*\})$", re.MULTILINE)
_TEXT = re.compile(r'"text": "(.*?)", "')


class ListThenRemove:
    """Stands in for M3A's model on ``terminal``: types ``ls``, then ``rm`` the file the goal names.

    The page makes its goal and its files when the task starts, so the
    commands are read off the action-selection prompt: the goal, the
    terminal's input (the one editable element), and the listing ``ls``
    printed. A summary call gets a fixed line.
    """

    inference_kind = "chat"
    model = name = "terminal-typist"

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: object = None
    ) -> ChatInvokeCompletion[str]:
        text = prompt_text(messages)
        if M3A_GOAL_LINE not in text:
            return ChatInvokeCompletion(completion="Typed a command.", usage=None)
        goal = text.split(M3A_GOAL_LINE, 1)[1].split("\n", 1)[0]
        elements = [(int(index), line) for index, line in _ELEMENT.findall(text)]
        terminal = next(index for index, line in elements if '"is_editable": True' in line)
        command = "ls" if not self.commands else f"rm {_wanted(goal, elements)}"
        self.commands.append(command)
        return ChatInvokeCompletion(
            completion=(
                f"Reason: The terminal takes the command {command!r}.\n"
                f'Action: {{"action_type": "input_text", "text": "{command}", '
                f'"index": {terminal}}}'
            ),
            usage=None,
        )

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


def _wanted(goal: str, elements: list[tuple[int, str]]) -> str:
    """The file of ``ls``'s listing (the line after ``user$ ls``) that the goal asks to delete."""
    texts = [found.group(1) if (found := _TEXT.search(line)) else "" for _, line in elements]
    files = texts[texts.index("user$ ls") + 1].split()
    if "no file extension" in goal:
        return next(name for name in files if "." not in name)
    extension = goal.rsplit("extension ", 1)[1].rstrip(".")
    return next(name for name in files if name.endswith(extension))


async def test_the_terminal_takes_commands_typed_as_key_events(tmp_path: Path) -> None:
    task_id = "terminal/42/0"
    data = tmp_path / "data"
    write_task_list(data, "terminal", family="miniwob")
    loader = AndroidWorldTaskLoader(data_dir=str(data))
    context = loader.get_task_context(task_id)
    capability = androidworld_capability()
    assert capability.typing_backend.typing_id == "key_events"
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
    model = ListThenRemove()
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

    assert result.agent_done == "episode_over", (result.agent_done, result.error, model.commands)
    assert len(model.commands) == 2 and model.commands[0] == "ls", model.commands
    assert writer.run_dir is not None
    trace = RunTrace(writer.run_dir)
    (score,) = trace.verdicts().values()
    assert (score.status, score.score) == ("judged", 1.0), score.detail
    typed = [
        acted.effect
        for record in trace.step_records
        for acted in record.action_results
        if isinstance(acted.effect, AndroidTextEffect)
    ]
    assert [effect.entered_by for effect in typed] == ["key_events", "key_events"]
    print(f"terminal: {model.commands}; score {score.score}")
