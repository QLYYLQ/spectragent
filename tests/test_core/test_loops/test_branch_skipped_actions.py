"""A step stopped at a failed action reads the same live, in source history and restored.

The user's ruling of 2026-10-24 (``docs/architecture_design.md`` §4.1): the
Loop does not execute the actions after a failed one and the next prompt says
which. A branch shows that step as the source showed it: ``history_source:
source`` rebuilds the recorded prompts byte for byte; ``history_source:
restored`` records the restoration's own step, which ran what the source ran,
failed at the same action and carries the same skipped tail, retargeted onto
the restored page, so each agent writes the same sentence from it.

The fake site fails the click on its second element (and, for UI-TARS, the
click at the same place on the screenshot), so the failure is the same in the
source world and the relaunched one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.loops import BranchLoop, ReactLoop
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.reconstruction import recorded_form
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.restoration import Checkpoint
from tests._helpers.branch_browser import IMAGE, VIEWPORT, AgentKind, FakeBrowserCap, drive
from tests._helpers.trace_builder import as_chat

AGENTS: list[AgentKind] = ["browser_use", "skyvern", "ui_tars"]

#: UI-TARS writes points on the 64x36 screenshot: grid x 30 is viewport x 600.
_FAILING_X = 30 * VIEWPORT[0] // IMAGE[0]

SCRIPTS: dict[AgentKind, list[Any]] = {
    "browser_use": [
        {
            "memory": "two at once",
            "action": [{"browser.click": {"index": 102}}, {"browser.click": {"index": 101}}],
        },
        {"memory": "blue", "action": [{"browser.click": {"index": 101}}]},
        {"memory": "done", "action": [{"browser.done": {"extracted_content": "bought"}}]},
    ],
    "skyvern": [
        {
            "actions": [
                {"reasoning": "", "action": {"browser.click": {"index": 102}}},
                {"reasoning": "", "action": {"browser.click": {"index": 101}}},
            ]
        },
        {"actions": [{"reasoning": "", "action": {"browser.click": {"index": 101}}}]},
        {"actions": [{"reasoning": "", "action": {"browser.done": {"extracted_content": "b"}}}]},
    ],
    "ui_tars": [
        "Thought: two.\nAction: click(point='(30,10)')\nAction: click(point='(10,20)')",
        "Thought: the blue one.\nAction: click(point='(10,20)')",
        "Action: finished(content='bought')",
    ],
}

#: What each agent's next prompt says about the skipped action, as written in
#: the source world; the restored one names it on the restored page.
SAID: dict[AgentKind, str] = {
    "browser_use": (
        "<not_executed>Action 2 (browser.click) was not executed: action 1 failed.</not_executed>"
    ),
    "skyvern": '"not_executed": "action 1 failed"',
    "ui_tars": '"not_executed": {"actions": ["click(point=\'(10, 20)\')"], '
    '"because": "action 1 failed"}',
}


@pytest.fixture(autouse=True)
def second_target_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    execute = FakeBrowserCap.execute_action

    async def fails_on_it(self: FakeBrowserCap, action: Action) -> ActionResult:
        index = action.params.get("index")
        if action.name == "browser.click" and index == self.id_offset + 2:
            self.log.append(f"{action.name}({action.params})")
            return ActionResult(success=False, error=f"element {index} is covered")
        if (
            action.name == "browser.click_coordinate"
            and action.params.get("coordinate_x") == _FAILING_X
        ):
            self.log.append(f"{action.name}({action.params})")
            return ActionResult(success=False, error="the point is covered")
        return await execute(self, action)

    monkeypatch.setattr(FakeBrowserCap, "execute_action", fails_on_it)


def _source_loop() -> ReactLoop:
    return ReactLoop(
        max_steps=10, max_actions_per_step=2, parse_retries_per_step=1, today_override="2026-10-11"
    )


def _branch_loop(history_source: str) -> BranchLoop:
    return BranchLoop(
        max_actions_per_step=2,
        parse_retries_per_step=1,
        history_source=history_source,  # type: ignore[arg-type]
    )


def _text(messages: Any) -> str:
    return json.dumps(recorded_form(messages), ensure_ascii=False)


async def _source(tmp_path: Path, kind: AgentKind) -> RunTrace:
    outcome = await drive(
        tmp_path / "src",
        kind=kind,
        memory="simple",
        loop=_source_loop(),
        replies=SCRIPTS[kind],
        run_id="src",
    )
    assert outcome.result is not None and outcome.result.agent_done.value == "done"
    assert outcome.run_dir is not None
    # The page saw step 0's failed click and step 1's click; step 0's skipped
    # click never reached it.
    assert len([line for line in outcome.cap.log if "click" in line]) == 2
    trace = RunTrace(outcome.run_dir)
    first = trace.step(0)
    assert first is not None
    assert len(first.actions) == 2 and first.skipped_actions == 1
    assert [r.success for r in first.action_results] == [False]
    return trace


@pytest.mark.parametrize("kind", AGENTS)
async def test_the_next_prompt_says_which_action_was_not_executed(
    tmp_path: Path, kind: AgentKind
) -> None:
    source = await _source(tmp_path, kind)
    prompt = as_chat(source.llm_messages(1)[0]).messages
    assert SAID[kind] in _text(prompt).replace('\\"', '"')


@pytest.mark.parametrize("kind", AGENTS)
async def test_source_history_rebuilds_the_skipped_step_exactly(
    tmp_path: Path, kind: AgentKind
) -> None:
    source = await _source(tmp_path, kind)
    branch = await drive(
        tmp_path / "branch",
        kind=kind,
        memory="simple",
        loop=_branch_loop("source"),
        replies=[SCRIPTS[kind][1], SCRIPTS[kind][2]],
        run_id="b1",
        checkpoint=Checkpoint(trace_dir=str(source.trace_dir.resolve()), step=1),
    )
    assert branch.result is not None and branch.result.error is None, branch.result
    teacher = recorded_form(branch.teacher.prompts[0])
    recorded = recorded_form(as_chat(source.llm_messages(1)[0]).messages)
    assert teacher == recorded


@pytest.mark.parametrize("kind", AGENTS)
async def test_restored_history_says_it_the_same_way(tmp_path: Path, kind: AgentKind) -> None:
    source = await _source(tmp_path, kind)
    branch = await drive(
        tmp_path / "restored",
        kind=kind,
        memory="simple",
        loop=_branch_loop("restored"),
        replies=[SCRIPTS[kind][2]],
        run_id="restored",
        id_offset=500,
        checkpoint=Checkpoint(trace_dir=str(source.trace_dir.resolve()), step=1),
    )
    assert branch.result is not None and branch.result.error is None, branch.result
    assert branch.run_dir is not None
    restored = RunTrace(branch.run_dir).step(0)
    assert restored is not None and restored.origin == "restored"
    assert [r.success for r in restored.action_results] == [False]
    assert restored.skipped_actions == 1
    # Retargeted onto the restored page, never run: the relaunched world's handle.
    (skipped,) = restored.skipped()
    if kind == "ui_tars":
        assert skipped.params == {"coordinate_x": 200, "coordinate_y": 400}
    else:
        assert skipped.params == {"index": 501}
    # The restoration ran the failed click and nothing else; the teacher then finished.
    assert len([line for line in branch.cap.log if "click" in line]) == 1
    said = SAID[kind]
    if kind != "ui_tars":
        said = said.replace("101", "501")
    text = _text(branch.teacher.prompts[0]).replace('\\"', '"')
    assert said in text
    if kind == "skyvern":
        assert re.search(r'"index": 501\}\}, "not_executed": "action 1 failed"', text)
