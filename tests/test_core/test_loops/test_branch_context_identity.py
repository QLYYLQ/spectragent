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
- Skyvern's rendered action-history window, which it renders from the Loop's
  ``history`` fragment, is rebuilt.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest

from spectragent.core.capabilities.browser.cursor import ScreenshotCursor
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.hooks.restored_history import OmitSourceRepliesHook
from spectragent.core.llm.exceptions import StructuredOutputValidationError
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.reconstruction import recorded_form
from spectragent.schemas.restoration import Checkpoint
from tests._helpers.branch_browser import (
    TASK,
    AgentKind,
    Outcome,
    branch_loop,
    drive,
    source_loop,
)
from tests._helpers.trace_builder import as_chat


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
        recorded = recorded_form(as_chat(source.llm_messages(step)[0]).messages)
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
        recorded = recorded_form(as_chat(source.llm_messages(step)[0]).messages)
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
    # Not vacuous: by step 2 the window holds the two actions that ran — step
    # 0's click and the kept half of step 1's cut reply. The cut action never
    # ran, so it is not history (the window was agent-owned state appended at
    # parse time before 2026-10-20, and then listed it too).
    assert len(recorded_history[2]) == 2
    assert "'index': 103" not in "\n".join(recorded_history[2])


@pytest.mark.parametrize("kind", AGENTS)
@pytest.mark.parametrize("memory", MEMORIES)
async def test_shipped_agent_restored_history_uses_live_execution(
    tmp_path: Path, kind: AgentKind, memory: str
) -> None:
    _, source = await _source(tmp_path, kind, memory)
    loop = branch_loop()
    loop.history_source = "restored"
    outcome = await drive(
        tmp_path / "restored-branches",
        kind=kind,
        memory=memory,
        loop=loop,
        replies=[SCRIPTS[kind]()[-1]],
        run_id="restored",
        id_offset=500,
        checkpoint=Checkpoint(trace_dir=str(source.trace_dir.resolve()), step=2),
    )
    assert outcome.result is not None and outcome.result.agent_done.value == "done"
    assert outcome.run_dir is not None
    trace = RunTrace(outcome.run_dir)
    assert [record.origin for record in trace.step_records] == [
        "restored",
        "restored",
        "policy",
        "terminal",
    ]
    assert trace.llm_messages(0) == trace.llm_messages(1) == []
    text = str(recorded_form(outcome.teacher.prompts[0]))
    assert "actual_restored_execution" in text and "source_material" in text
    assert "not a new reply or thought" in text
    if kind != "ui_tars":
        first = trace.step(0)
        assert first is not None and first.source_history is not None
        assert first.actions[0].params["index"] == 501
        assert first.source_history.source_actions[0].params["index"] == 101
    assert all(record.llm_output == "" for record in trace.step_records[:2])


@pytest.mark.parametrize("history_source", ["source", "restored"])
async def test_cursor_model_view_follows_source_or_restored_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_source: Literal["source", "restored"],
) -> None:
    from tests._helpers.branch_browser import FakeBrowserCap

    resolve = FakeBrowserCap.resolve_fragment

    async def with_cursor(self, key):
        fragment = await resolve(self, key)
        if isinstance(fragment, ScreenshotObs):
            # Source and relaunched worlds have identical clean pixels, but
            # different confirmed cursor positions. This is presentation only.
            fragment.cursor = ScreenshotCursor(
                position=(10, 10) if self.id_offset == 100 else (50, 25),
            )
        return fragment

    monkeypatch.setattr(FakeBrowserCap, "resolve_fragment", with_cursor)
    _, source = await _source(tmp_path, "ui_tars", "simple")
    loop = branch_loop()
    loop.history_source = history_source
    outcome = await drive(
        tmp_path / "cursor-branch",
        kind="ui_tars",
        memory="simple",
        loop=loop,
        replies=[SCRIPTS["ui_tars"]()[-1]],
        run_id="cursor-branch",
        id_offset=500,
        checkpoint=Checkpoint(trace_dir=str(source.trace_dir.resolve()), step=2),
    )
    assert outcome.result is not None and outcome.result.error is None
    assert outcome.run_dir is not None
    branch_trace = RunTrace(outcome.run_dir)
    source_image = source.observation_fragment(0, "browser.screenshot")
    live_image = branch_trace.observation_fragment(0, "browser.screenshot")
    assert isinstance(source_image, ScreenshotObs) and isinstance(live_image, ScreenshotObs)
    assert source_image.image_bytes == live_image.image_bytes
    assert source_image.model_image_bytes() != live_image.model_image_bytes()
    import base64

    from spectragent.core.llm.messages import ContentPartImageParam

    images = [
        part.image_url.url
        for message in outcome.teacher.prompts[0]
        if isinstance(message.content, list)
        for part in message.content
        if isinstance(part, ContentPartImageParam)
    ]
    expected = source_image if history_source == "source" else live_image
    assert base64.b64decode(images[0].split(",", 1)[1]) == expected.model_image_bytes()


#: A UI-TARS source whose first thought quotes its task (as real thoughts do), and
#: whose checkpoint-step thought is the one a branch at step 2 must decide anew.
QUOTING_SOURCE = [
    "Thought: to put the blue kettle in the cart, I open the blue one.\n"
    "Action: click(point='(10,20)')",
    "Thought: now on to the checkout.\nAction: click(point='(20,20)')",
    "Thought: the cart holds the kettle.\nAction: finished(content='bought')",
]
VARIANT_TASK = "find out how long the kettle's warranty lasts"


def _message_texts(messages: list[Any]) -> list[tuple[str, str]]:
    """Each message's role and its text parts (images left out)."""
    texts: list[tuple[str, str]] = []
    for message in recorded_form(messages):
        content = message["content"]
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        texts.append((message["role"], str(content)))
    return texts


async def test_ui_tars_restored_branch_under_a_task_variant_sees_the_source_thoughts(
    tmp_path: Path,
) -> None:
    """What the first policy prompt of a task-variant branch holds (UI-TARS, restored history).

    - The task is the variant's: it is the one task in the system seed.
    - The source's task text appears only where the source's own replies quoted it: restored
      history renders each restored step's recorded reply, thought included, as
      ``source_material.recorded_reply`` (``spectragent/agents/prompt_context.py``
      ``restored_history_message``, which ``UITarsAgent.build_restored_history`` appends).
    - The checkpoint step's own source reply is not rendered: the Loop hands it only to
      prompt-contribution functions (``ReactPromptContext.source_reply``).
    """
    source_outcome = await drive(
        tmp_path / "src",
        kind="ui_tars",
        memory="simple",
        loop=source_loop(),
        replies=list(QUOTING_SOURCE),
        run_id="src",
    )
    assert source_outcome.result is not None and source_outcome.result.agent_done.value == "done"
    assert source_outcome.run_dir is not None
    loop = branch_loop()
    loop.history_source = "restored"
    outcome = await drive(
        tmp_path / "variant-branch",
        kind="ui_tars",
        memory="simple",
        loop=loop,
        replies=["Action: finished(content='two years')"],
        run_id="variant-branch",
        id_offset=500,
        checkpoint=Checkpoint(
            trace_dir=str(source_outcome.run_dir.resolve()), step=2, task_variant="warranty"
        ),
        task=VARIANT_TASK,
    )
    assert outcome.result is not None and outcome.result.error is None, outcome.result
    texts = _message_texts(outcome.teacher.prompts[0])

    seed_role, seed = texts[0]
    assert seed_role == "system" and VARIANT_TASK in seed and TASK not in seed
    assert [role for role, text in texts if VARIANT_TASK in text] == ["system"]

    quoting = [text for _role, text in texts if TASK in text]
    assert len(quoting) == 1, "the source task appears once: in step 0's recorded reply"
    assert "Restored execution history" in quoting[0]
    assert '"recorded_reply": "Thought: to put the blue kettle in the cart' in quoting[0]
    assert any("now on to the checkout" in text for _role, text in texts), "step 1's thought"
    assert not any("the cart holds the kettle" in text for _role, text in texts)


async def test_omit_source_replies_keeps_the_source_thoughts_out_of_a_restored_prompt(
    tmp_path: Path,
) -> None:
    """The ablation hook: the restored actions stay, the source model's replies do not.

    Same source and variant branch as above, with ``omit_source_replies`` woven: no
    recorded reply reaches the first policy prompt (so the source task, which only a
    reply quoted, is gone too), the actually restored execution is still described, and
    the trace keeps the replies as provenance in ``source_history``.
    """
    source_outcome = await drive(
        tmp_path / "src",
        kind="ui_tars",
        memory="simple",
        loop=source_loop(),
        replies=list(QUOTING_SOURCE),
        run_id="src",
    )
    assert source_outcome.run_dir is not None
    loop = branch_loop()
    loop.history_source = "restored"
    outcome = await drive(
        tmp_path / "variant-branch",
        kind="ui_tars",
        memory="simple",
        loop=loop,
        replies=["Action: finished(content='two years')"],
        run_id="variant-branch",
        id_offset=500,
        checkpoint=Checkpoint(
            trace_dir=str(source_outcome.run_dir.resolve()), step=2, task_variant="warranty"
        ),
        task=VARIANT_TASK,
        extra_hooks=[OmitSourceRepliesHook()],
    )
    assert outcome.result is not None and outcome.result.error is None, outcome.result
    texts = [text for _role, text in _message_texts(outcome.teacher.prompts[0])]

    assert not any(TASK in text for text in texts), "no reply quoting the source task"
    assert not any("now on to the checkout" in text for text in texts)
    restored = [text for text in texts if "Restored execution history" in text]
    assert len(restored) == 2, "both restored steps are still described"
    assert all('"recorded_reply": ""' in text for text in restored)
    assert all('"actual_restored_execution"' in text for text in restored)

    assert outcome.run_dir is not None
    trace = RunTrace(outcome.run_dir)
    kept = [record.source_history for record in trace.step_records if record.origin == "restored"]
    assert [history.source_reply.splitlines()[0] for history in kept if history is not None] == [
        "Thought: to put the blue kettle in the cart, I open the blue one.",
        "Thought: now on to the checkout.",
    ]
