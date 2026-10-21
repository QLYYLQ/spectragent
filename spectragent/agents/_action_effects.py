"""Render an action's measured effect: its ``describe()`` line, never its fields.

``docs/architecture_design.md`` §4.5, "动作效果". An ``ActionResult`` may carry
the effect its action measured while it ran. Every Agent renders that effect as
the one deterministic line ``describe()`` writes, so a cross-platform Agent
never needs to know a concrete effect type and no raw effect field reaches a
prompt. Whether the line is shown at all is each Agent's ``show_action_effects``
switch; the trace records the effect either way.

Every point the line states is written by the Agent's ``write_point``
(:data:`~spectragent.schemas.action.PointWriter`): viewport units by default, the
grid its model writes points on for an Agent that passes its own (§4.1,
"模型看到的每个点都写在它写点的那个网格上").

The sensitivity marker (``typed_into_sensitive_field``) is trace-only evidence
and is left out of every result a prompt shows, as before.
"""

from __future__ import annotations

from typing import Any

from spectragent.schemas.action import ActionResult, PointWriter, write_viewport_point

#: Result fields no prompt shows: the sensitivity marker is trace-only, and the
#: effect is shown only as its ``describe()`` line.
PROMPT_EXCLUDED_RESULT_FIELDS: frozenset[str] = frozenset({"typed_into_sensitive_field", "effect"})


def effect_line(
    result: ActionResult, *, shown: bool, write_point: PointWriter = write_viewport_point
) -> str | None:
    """*result*'s effect as its one ``describe()`` line, or ``None``.

    ``None`` when effects are not shown, or when the action reported no effect
    (it declares none, or did not get as far as anything measurable). Every
    point the line states is written by *write_point*.
    """
    if not shown or result.effect is None:
        return None
    return result.effect.describe(write_point=write_point)


def result_facts(
    result: ActionResult, *, show_effects: bool, write_point: PointWriter = write_viewport_point
) -> dict[str, Any]:
    """*result* as the JSON object a prompt shows.

    Every field but :data:`PROMPT_EXCLUDED_RESULT_FIELDS`, plus ``"effect"``
    holding the effect's ``describe()`` line (its points written by
    *write_point*) when effects are shown and the action reported one.
    """
    facts = result.model_dump(mode="json", exclude=set(PROMPT_EXCLUDED_RESULT_FIELDS))
    line = effect_line(result, shown=show_effects, write_point=write_point)
    if line is not None:
        facts["effect"] = line
    return facts


__all__ = ["PROMPT_EXCLUDED_RESULT_FIELDS", "effect_line", "result_facts"]
