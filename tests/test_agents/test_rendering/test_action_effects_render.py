"""An action's effect reaches a prompt only as its ``describe()`` line.

``docs/architecture_design.md`` §4.5, "动作效果". ``agents/rendering/action_effects.py``
is the one place a result becomes prompt facts: the effect's raw fields and the
sensitivity marker never do, and ``show_action_effects`` off means no effect
line at all — while the effect stays on the result, for the trace.
"""

from __future__ import annotations

import json

from spectragent.agents.rendering.action_effects import (
    PROMPT_EXCLUDED_RESULT_FIELDS,
    effect_line,
    result_facts,
)
from spectragent.agents.rendering.prompt_context import restored_history_message
from spectragent.core.capabilities.browser.effects import ScrollEffect, ScrollOffset
from spectragent.schemas.action import (
    Action,
    ActionEffect,
    ActionResult,
    PointWriter,
    write_viewport_point,
)
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.prompt import ReactPromptContext, RestoredHistory
from spectragent.schemas.restoration import RestoredAction

#: A raw field value no ``describe()`` line contains: finding it in a prompt
#: means a field was dumped.
RAW = "RAW-EFFECT-FIELD"


class ProbeEffect(ActionEffect):
    moved: int
    raw_marker: str = RAW

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        return f"Probe moved {self.moved} px."


def _result(**overrides: object) -> ActionResult:
    fields: dict[str, object] = {
        "success": True,
        "typed_into_sensitive_field": True,
        "effect": ProbeEffect(effect_id="probe.move", moved=40),
    }
    fields.update(overrides)
    return ActionResult.model_validate(fields)


def test_the_effect_line_is_describe() -> None:
    assert effect_line(_result(), shown=True) == "Probe moved 40 px."
    scroll = ScrollEffect(
        effect_id="browser.scroll",
        direction="down",
        container="page",
        before=ScrollOffset(x=0, y=0),
        after=ScrollOffset(x=0, y=600),
        at_boundary=False,
    )
    assert effect_line(_result(effect=scroll), shown=True) == scroll.describe()


def test_no_line_when_not_shown_or_not_measured() -> None:
    assert effect_line(_result(), shown=False) is None
    assert effect_line(_result(effect=None), shown=True) is None


def test_result_facts_carry_the_line_and_never_the_fields() -> None:
    facts = result_facts(_result(), show_effects=True)
    assert facts == {
        "success": True,
        "error": None,
        "is_done": False,
        "extracted_content": None,
        "effect": "Probe moved 40 px.",
    }
    assert RAW not in json.dumps(facts)
    assert {"typed_into_sensitive_field", "effect"} == PROMPT_EXCLUDED_RESULT_FIELDS


def test_result_facts_without_effects_shown_are_the_result_without_an_effect() -> None:
    hidden = result_facts(_result(), show_effects=False)
    unmeasured = result_facts(_result(effect=None), show_effects=True)
    assert hidden == unmeasured
    assert "effect" not in hidden


def test_switching_effects_off_keeps_the_effect_on_the_result() -> None:
    """The switch decides what the model reads, not what the trace records."""
    result = _result()
    result_facts(result, show_effects=False)
    assert result.effect is not None
    assert "effect" in result.model_dump(mode="json")


def _restored() -> RestoredHistory:
    source = Action(name="probe.move", params={"by": 10})
    actual = Action(name="probe.move", params={"by": 12})
    result = _result(effect=ProbeEffect(effect_id="probe.move", moved=12))
    return RestoredHistory(
        source_run_id="source-run",
        source_step=1,
        source_reply="old reply",
        source_actions=[source],
        actions=[actual],
        results=[result],
        executions=[
            RestoredAction(
                recorded=source,
                executed=actual,
                method="identity",
                recorded_success=True,
                live_success=True,
                result=result,
            )
        ],
    )


def _restored_facts(*, shown: bool) -> dict[str, object]:
    message = restored_history_message(
        _restored(),
        show_action_effects=shown,
        context=ReactPromptContext(step=1, phase="history"),
        action_space=ActionSpace(),
    )
    assert RAW not in message.text
    return json.loads(message.text.split("\n", 1)[1])


def test_restored_history_shows_the_effect_measured_while_restoring() -> None:
    facts = _restored_facts(shown=True)
    (executed,) = facts["actual_restored_execution"]  # type: ignore[misc]
    assert executed["result"]["effect"] == "Probe moved 12 px."
    assert "typed_into_sensitive_field" not in executed["result"]


def test_restored_history_without_effects_shown_has_no_effect() -> None:
    facts = _restored_facts(shown=False)
    (executed,) = facts["actual_restored_execution"]  # type: ignore[misc]
    assert "effect" not in executed["result"]
