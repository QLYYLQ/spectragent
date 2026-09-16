"""Action-related data contracts.

See docs/architecture_design.md §4.5 for the full Action design.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionResult(BaseModel):
    """Action execution result."""

    success: bool
    error: str | None = None
    is_done: bool = False
    extracted_content: str | None = None


class ActionDescriptor(BaseModel):
    """Describes an available action for LLM prompt building. Immutable.

    ``name`` is the action's fully-qualified registry key
    (``browser.click``), which is what ``env.act`` routes on and what the
    trace records. Actions do not write it out by hand — they return
    ``ActionDescriptor(name=self.action_id, ...)``, so the advertised name
    and the routing key cannot drift.

    The two parameter fields are **one** contract seen twice, not a simple
    mode and a rich one. Every action declares a typed ``Params`` model;
    ``ActionBase.describe`` compiles it once
    (``core/actions/params.py``) into:

    - ``params_schema`` — the JSON Schema a provider constrains on, and
      what an agent compiles into its closed per-run output model;
    - ``params`` — the prose the prompt shows, *rendered from that same
      schema* (``{"url": "str -- where to go"}``), never hand-written.

    So they cannot describe different parameters, and a param-level
    ``RequiresFragment`` that drops a param this run cannot honour drops
    it from both at once. ``params_schema`` is populated on every
    registered action; ``None`` means an action that has not been given a
    ``Params`` model yet, not a simpler kind of action.

    There is deliberately no conversion to a provider function-calling
    schema here; see ``docs/action_descriptor_vs_tool_definition.md``.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    params: dict[str, str] = Field(default_factory=dict)
    params_schema: dict[str, Any] | None = None


class Action(BaseModel):
    """Parsed action from LLM output."""

    name: str
    params: dict[str, Any] = Field(default_factory=dict)
