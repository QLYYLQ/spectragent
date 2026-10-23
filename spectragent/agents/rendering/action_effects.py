"""Render an action's measured effect: its ``describe()`` line, never its fields.

``docs/architecture_design.md`` §4.5, "动作效果". An ``ActionResult`` may carry
the effect its action measured while it ran. Every Agent renders that effect as
the one deterministic line ``describe()`` writes, so a cross-platform Agent
never needs to know a concrete effect type and no raw effect field reaches a
prompt. Whether the line is shown at all is each Agent's ``show_action_effects``
switch; the trace records the effect either way.

Every point the line states is written by the ``write_point`` the Agent passes
(:data:`~spectragent.schemas.action.PointWriter`): viewport units by default, and
the framework's writer for the step's frame where the run's model writes points
on a grid of its own (§4.1, "坐标系归框架").

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
    result: ActionResult,
    *,
    show_effects: bool,
    write_point: PointWriter = write_viewport_point,
    max_content_chars: int | None = None,
) -> dict[str, Any]:
    """*result* as the JSON object a prompt shows.

    Every field but :data:`PROMPT_EXCLUDED_RESULT_FIELDS`, plus ``"effect"``
    holding the effect's ``describe()`` line (its points written by
    *write_point*) when effects are shown and the action reported one.

    *max_content_chars* bounds ``extracted_content``, the text an action
    returned (a page's text, an element's HTML): longer content is cut there
    and ends with a note saying how much of it is shown, so a model never
    reads a cut text as the whole of it. ``None``: shown whole.
    """
    facts = result.model_dump(mode="json", exclude=set(PROMPT_EXCLUDED_RESULT_FIELDS))
    content = facts.get("extracted_content")
    if max_content_chars is not None and isinstance(content, str):
        facts["extracted_content"] = bounded_content(content, max_content_chars)
    line = effect_line(result, shown=show_effects, write_point=write_point)
    if line is not None:
        facts["effect"] = line
    return facts


def bounded_content(content: str, max_chars: int) -> str:
    """*content* cut to *max_chars* characters, with a note when it was cut."""
    if len(content) <= max_chars:
        return content
    return f"{content[:max_chars]}{CONTENT_CUT_NOTE.format(shown=max_chars, total=len(content))}"


#: Appended to returned content cut by :func:`bounded_content`.
CONTENT_CUT_NOTE = " ... [{shown} of {total} characters shown]"


__all__ = [
    "CONTENT_CUT_NOTE",
    "PROMPT_EXCLUDED_RESULT_FIELDS",
    "bounded_content",
    "effect_line",
    "result_facts",
]
