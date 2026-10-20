"""A branch renders the effects that were actually measured — recorded or restored.

``docs/architecture_design.md`` §4.5, "动作效果": effects persist on
``StepRecord.action_results`` and reach the agent through ``loop.history``.
``history_source: source`` rebuilds each prefix prompt from the recorded
records, so the effect lines the source showed are shown again and the
checkpoint's prompt equals the recorded one byte for byte;
``history_source: restored`` shows the effects measured while restoring,
never a copy of the source's. Both go through the agents' one renderer.

The fake site attaches a test effect to every action it executes whose value
depends on the world (``id_offset``): the source world measures ``10xx``, the
relaunched world ``50xx``, so a prompt says which world its effects came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.reconstruction import recorded_form
from spectragent.schemas.action import Action, ActionDescriptor, ActionEffect, ActionResult
from spectragent.schemas.restoration import Checkpoint
from tests._helpers.branch_browser import (
    AgentKind,
    FakeBrowserCap,
    branch_loop,
    drive,
    source_loop,
)
from tests._helpers.trace_builder import as_chat
from tests.test_core.test_loops.test_branch_context_identity import PARSED_AT, SCRIPTS

AGENTS: list[AgentKind] = ["browser_use", "skyvern", "ui_tars"]


class WorldEffect(ActionEffect):
    """A test effect whose value says which world measured it."""

    measured: int

    def describe(self) -> str:
        return f"World measured {self.measured}."


def _register_effect_action() -> None:
    """The action the recorded effects name, so the trace reader can rebuild them."""

    @dataclass
    class WorldProbe(ActionBase):
        __registry_key__ = "worldprobe.mark"
        Effect = WorldEffect

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Mark the world.", **context)


@pytest.fixture
def measured_world(monkeypatch: pytest.MonkeyPatch) -> None:
    _register_effect_action()
    execute = FakeBrowserCap.execute_action

    async def with_effect(self: FakeBrowserCap, action: Action) -> ActionResult:
        result = await execute(self, action)
        if result.is_done:
            return result
        effect = WorldEffect(
            effect_id="worldprobe.mark", measured=self.id_offset * 10 + self.clicks
        )
        return result.model_copy(update={"effect": effect})

    monkeypatch.setattr(FakeBrowserCap, "execute_action", with_effect)


async def _source(tmp_path: Path, kind: AgentKind) -> RunTrace:
    outcome = await drive(
        tmp_path / "src",
        kind=kind,
        memory="simple",
        loop=source_loop(),
        replies=SCRIPTS[kind](),
        run_id="src",
    )
    assert outcome.result is not None and outcome.result.agent_done.value == "done"
    assert outcome.run_dir is not None
    trace = RunTrace(outcome.run_dir)
    effect = trace.step(0).action_results[0].effect  # type: ignore[union-attr]
    assert isinstance(effect, WorldEffect)
    return trace


@pytest.mark.usefixtures("measured_world")
@pytest.mark.parametrize("kind", AGENTS)
async def test_source_history_shows_the_recorded_effects_exactly(
    tmp_path: Path, kind: AgentKind
) -> None:
    source = await _source(tmp_path, kind)
    step = 2
    script = SCRIPTS[kind]()
    branch = await drive(
        tmp_path / "branch",
        kind=kind,
        memory="simple",
        loop=branch_loop(),
        replies=[script[PARSED_AT[step]], script[-1]],
        run_id="b2",
        id_offset=100,
        checkpoint=Checkpoint(trace_dir=str(source.trace_dir.resolve()), step=step),
    )
    assert branch.result is not None and branch.result.error is None, branch.result
    teacher = recorded_form(branch.teacher.prompts[0])
    recorded = recorded_form(as_chat(source.llm_messages(step)[0]).messages)
    assert teacher == recorded
    # Not vacuous: the recorded prompt carries the source world's effect lines.
    assert "World measured 10" in str(recorded)


@pytest.mark.usefixtures("measured_world")
@pytest.mark.parametrize("kind", AGENTS)
async def test_restored_history_shows_the_effects_measured_while_restoring(
    tmp_path: Path, kind: AgentKind
) -> None:
    source = await _source(tmp_path, kind)
    loop = branch_loop()
    loop.history_source = "restored"
    outcome = await drive(
        tmp_path / "restored",
        kind=kind,
        memory="simple",
        loop=loop,
        replies=[SCRIPTS[kind]()[-1]],
        run_id="restored",
        id_offset=500,
        checkpoint=Checkpoint(trace_dir=str(source.trace_dir.resolve()), step=2),
    )
    assert outcome.result is not None and outcome.result.agent_done.value == "done"
    text = str(recorded_form(outcome.teacher.prompts[0]))
    assert "World measured 50" in text
    assert "World measured 10" not in text
    # The restored records the trace keeps carry the restored world's effects too.
    assert outcome.run_dir is not None
    first = RunTrace(outcome.run_dir).step(0)
    assert first is not None and first.origin == "restored"
    effect = first.action_results[0].effect
    assert isinstance(effect, WorldEffect) and effect.measured // 1000 == 5
