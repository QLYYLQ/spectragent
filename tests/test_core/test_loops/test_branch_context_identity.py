"""A branch's policy step decides on the source's context — for the real browser agents.

The acceptance criterion of the branching decision
(``docs/architecture_design.md`` §4.1 "`BranchLoop`"): the conversation before
the checkpoint is rebuilt **from the source trace**, through the agent's own
slots and ReAct's memory protocol, and only the checkpoint step's prompt is
built from the restored, live observation. For ``browser_use`` (``dom_vision``),
``skyvern`` and ``ui_tars``, each with a ``simple`` and a ``sliding_window``
memory, over a source with an in-step parse retry, a structured reply the
provider refused (the structured agents) and a multi-action reply cut to one:

- in a deterministic world, the teacher's first prompt at the checkpoint is
  the source's recorded prompt for that step, message by message, at every
  checkpoint;
- in a relaunched world (every element handle fresh), every message but the
  last still equals the source's, and the last is what the agent built from
  the live observation — which, for an agent that reads the DOM, is not the
  recorded one;
- Skyvern's ``_action_history`` (agent state no memory holds) is rebuilt.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.llm.exceptions import StructuredOutputValidationError
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.reconstruction import recorded_form
from spectragent.schemas.restoration import Checkpoint
from tests._helpers.branch_browser import AgentKind, Outcome, branch_loop, drive, source_loop


def _refusal(raw: str) -> StructuredOutputValidationError:
    return StructuredOutputValidationError("the reply does not fit the schema", raw_output=raw)


#: Per agent: the source's replies in call order. Step 0 takes two calls (a
#: reply the agent's parser refuses, then one it reads); step 1 is a
#: two-action reply cut to one; step 2 one action; step 3 done.
SCRIPTS: dict[str, Callable[[], list[Any]]] = {
    "browser_use": lambda: [
        _refusal('{"memory": "", "action": [{"browser.fly": {}}]}'),
        {"memory": "blue first", "action": [{"browser.click": {"index": 101}}]},
        {
            "memory": "two at once",
            "action": [{"browser.click": {"index": 102}}, {"browser.click": {"index": 103}}],
        },
        {"memory": "checkout", "action": [{"browser.click": {"index": 103}}]},
        {
            "memory": "done",
            "action": [{"browser.done": {"extracted_content": "bought", "success": True}}],
        },
    ],
    "skyvern": lambda: [
        _refusal('{"actions": "nope"}'),
        {"actions": [{"action_type": "browser.click", "index": 101, "reasoning": "blue"}]},
        {
            "actions": [
                {"action_type": "browser.click", "index": 102},
                {"action_type": "browser.click", "index": 103},
            ]
        },
        {"actions": [{"action_type": "browser.click", "index": 103}]},
        {"actions": [], "is_done": True, "extracted_content": "bought"},
    ],
    "ui_tars": lambda: [
        "I will just describe the page instead.",
        "Thought: the blue one.\nAction: click(point='(10,20)')",
        "Thought: two.\nAction: click(point='(10,20)')\nAction: click(point='(30,10)')",
        "Thought: checkout.\nAction: click(point='(20,20)')",
        "Action: finished(content='bought')",
    ],
}
#: The index in the script of the reply each step's last call parsed.
PARSED_AT = {0: 1, 1: 2, 2: 3, 3: 4}
STEPS = 4
AGENTS: list[AgentKind] = ["browser_use", "skyvern", "ui_tars"]
MEMORIES = ["simple", "sliding_window"]


async def _source(tmp_path: Path, kind: AgentKind, memory: str) -> tuple[Outcome, RunTrace]:
    outcome = await drive(
        tmp_path / "src",
        kind=kind,
        memory=memory,
        loop=source_loop(),
        replies=SCRIPTS[kind](),
        run_id="src",
    )
    assert outcome.result is not None and outcome.result.agent_done.value == "done"
    assert outcome.run_dir is not None
    trace = RunTrace(outcome.run_dir)
    # The source has what the replay must reproduce: a re-ask, a refused
    # structured reply (structured agents), a truncation.
    assert len(trace.llm_messages(0)) == 2
    first = trace.llm_messages(0)[0]
    if kind == "ui_tars":
        assert first.status == "completed"
    else:
        assert (first.status, first.failure.error if first.failure else None) == (
            "failed",
            "StructuredOutputValidationError",
        )
    assert trace.step(1).truncated_actions == 1  # type: ignore[union-attr]
    assert trace.step_count == STEPS
    # The vision agents' prompts carry screenshots inline: the source stores each one once
    # and its call records refer to it, so every comparison below — and the branch's own
    # check of the rebuilt context — reads prompts put back together
    # (docs/architecture_design.md §5.3).
    if kind != "skyvern":
        assert trace.manifest.prompt_images
    return outcome, trace


async def _branch(
    tmp_path: Path, kind: AgentKind, memory: str, source: RunTrace, step: int, *, id_offset: int
) -> Outcome:
    script = SCRIPTS[kind]()
    outcome = await drive(
        tmp_path / "branches",
        kind=kind,
        memory=memory,
        loop=branch_loop(),
        replies=[script[PARSED_AT[step]], script[-1]],
        run_id=f"b{step}-{id_offset}",
        id_offset=id_offset,
        checkpoint=Checkpoint(trace_dir=str(source.trace_dir.resolve()), step=step),
    )
    assert outcome.result is not None, "the branch raised"
    assert outcome.result.error is None, outcome.result.error
    return outcome


@pytest.mark.parametrize("memory", MEMORIES)
@pytest.mark.parametrize("kind", AGENTS)
async def test_policy_context_equals_source(tmp_path: Path, kind: AgentKind, memory: str) -> None:
    _, source = await _source(tmp_path, kind, memory)
    for step in range(STEPS):
        branch = await _branch(tmp_path, kind, memory, source, step, id_offset=100)
        teacher = recorded_form(branch.teacher.prompts[0])
        recorded = recorded_form(source.llm_messages(step)[0].messages)
        assert teacher == recorded, f"step {step}: the teacher's context is not the source's"


@pytest.mark.parametrize("memory", MEMORIES)
@pytest.mark.parametrize("kind", AGENTS)
async def test_history_from_recording_prompt_from_live(
    tmp_path: Path, kind: AgentKind, memory: str
) -> None:
    _, source = await _source(tmp_path, kind, memory)
    for step in range(STEPS):
        branch = await _branch(tmp_path, kind, memory, source, step, id_offset=500)
        teacher = recorded_form(branch.teacher.prompts[0])
        recorded = recorded_form(source.llm_messages(step)[0].messages)
        # Everything before the checkpoint's prompt comes from the recording.
        assert teacher[:-1] == recorded[:-1], f"step {step}: the rebuilt history differs"
        # The checkpoint's prompt is what the agent built from the live page.
        live_prompt, _ = branch.prompts_built[-1]
        assert teacher[-1] == recorded_form(live_prompt)[-1]
        if kind != "ui_tars":
            # The live page's element handles are fresh, so the prompt is not
            # the recorded one: it was not copied from the trace.
            assert teacher[-1] != recorded[-1]
            assert "[501]" in str(teacher[-1]["content"])
        # Recorded index actions ran against the fresh handles.
        if kind != "ui_tars" and step > 0:
            assert "'index': 501" in branch.cap.log[0]


@pytest.mark.parametrize("memory", MEMORIES)
async def test_skyvern_action_history_rebuilt(tmp_path: Path, memory: str) -> None:
    source_outcome, source = await _source(tmp_path, "skyvern", memory)
    recorded_history = [snapshot for _, snapshot in source_outcome.prompts_built]
    for step in range(STEPS):
        branch = await _branch(tmp_path, "skyvern", memory, source, step, id_offset=100)
        _, at_policy_step = branch.prompts_built[-1]
        assert at_policy_step == recorded_history[step]
    # Not vacuous: by step 2 the history holds both actions of the cut reply.
    assert len(recorded_history[2]) == 3
