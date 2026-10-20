"""Render an action's measured effect: its ``describe()`` line, never its fields.

``docs/architecture_design.md`` §4.5, "动作效果". An ``ActionResult`` may carry
the effect its action measured while it ran. Every Agent renders that effect as
the one deterministic line ``describe()`` writes, so a cross-platform Agent
never needs to know a concrete effect type and no raw effect field reaches a
prompt. Whether the line is shown at all is each Agent's ``show_action_effects``
switch; the trace records the effect either way.

The sensitivity marker (``typed_into_sensitive_field``) is trace-only evidence
and is left out of every result a prompt shows, as before.
"""

from __future__ import annotations

from typing import Any

from spectragent.schemas.action import ActionResult

#: Result fields no prompt shows: the sensitivity marker is trace-only, and the
#: effect is shown only as its ``describe()`` line.
PROMPT_EXCLUDED_RESULT_FIELDS: frozenset[str] = frozenset({"typed_into_sensitive_field", "effect"})


def effect_line(result: ActionResult, *, shown: bool) -> str | None:
    """*result*'s effect as its one ``describe()`` line, or ``None``.

    ``None`` when effects are not shown, or when the action reported no effect
    (it declares none, or did not get as far as anything measurable).
    """
    if not shown or result.effect is None:
        return None
    return result.effect.describe()


def result_facts(result: ActionResult, *, show_effects: bool) -> dict[str, Any]:
    """*result* as the JSON object a prompt shows.

    Every field but :data:`PROMPT_EXCLUDED_RESULT_FIELDS`, plus ``"effect"``
    holding the effect's ``describe()`` line when effects are shown and the
    action reported one.
    """
    facts = result.model_dump(mode="json", exclude=set(PROMPT_EXCLUDED_RESULT_FIELDS))
    line = effect_line(result, shown=show_effects)
    if line is not None:
        facts["effect"] = line
    return facts


__all__ = ["PROMPT_EXCLUDED_RESULT_FIELDS", "effect_line", "result_facts"]
