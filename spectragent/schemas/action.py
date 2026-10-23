"""Action-related data contracts.

See docs/architecture_design.md §4.5 for the full Action design.
"""

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator, model_validator

#: A fact an action effect could not report as a value. ``"unknown"``: measuring
#: it was attempted and failed or ran out of the measurement budget.
#: ``"unavailable"``: it cannot be measured here (the platform has no such
#: fact, or it lies where the capability cannot look). Neither is ``None`` or
#: ``0``: a measured zero is a value, and the three stay distinct in the trace
#: and in :meth:`ActionEffect.describe`.
Unmeasured = Literal["unknown", "unavailable"]

#: How a prompt writes a point it states: the point's ``(x, y)`` in the
#: capability's viewport units in, its text out. The facts stay in viewport
#: units; only the writing is the renderer's. An Agent whose model writes
#: points on another grid passes a writer for that grid, so every point the
#: model reads is on the grid it writes (``docs/architecture_design.md`` §4.1,
#: "模型看到的每个点都写在它写点的那个网格上").
PointWriter = Callable[[int, int], str]


def write_viewport_point(x: int, y: int) -> str:
    """The default :data:`PointWriter`: ``(x, y)`` in viewport units, as written before."""
    return f"({x}, {y})"


class ActionEffect(BaseModel):
    """What one action measurably did — the output-fact twin of its ``Params``.

    An action class may declare a nested ``Effect`` model (a subclass of this
    one); it measures the facts while it executes — before and after its own
    dispatch, so they belong to that action alone — and returns them in
    :attr:`ActionResult.effect`. Several actions may share one effect class.
    The model is capability-specific; this base names no capability field.

    ``effect_id`` is the fully-qualified key of the action that produced it
    (``"browser.scroll"``). It is how a recorded effect is read back: the trace
    reader looks the key up in the action registry and validates the payload
    with that action's ``Effect`` (``core/trace/effects.py``). This package
    never imports an action class.

    :meth:`describe` is the one line an Agent renders: English facts derived
    deterministically from this object's typed fields alone, with no judgement
    about the task. Unknown and unavailable facts are said as such; a
    sensitive field's content and length are never said. Agents render that
    line rather than dumping fields, so an Agent that is not specific to a
    platform never has to know a concrete effect type
    (``docs/architecture_design.md`` §4.5, "动作效果").

    Every *point* the line states is written by the caller's ``write_point``
    (:data:`PointWriter`), so an Agent whose model writes points on its own
    grid reads them on that grid; the default writes viewport units as
    before. A distance (a scroll's offset) is not a point and stays in the
    viewport's pixels, its unit stated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect_id: str = Field(min_length=1)

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        """One deterministic English line of the measured facts, for a prompt.

        Args:
            write_point: Writes every point the line states (see the class
                docstring). Every override accepts it: Agents always pass one.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement describe()")


class ActionResult(BaseModel):
    """Action execution result.

    ``typed_into_sensitive_field`` is recorded when an action enters text:
    whether the field it went into holds a secret (for the browser, the
    shared sensitivity rule in ``dom/sensitivity.py``: a password,
    one-time-code or payment-card field, judged on the focused element right
    before typing). ``None`` when the action entered no text, or when the
    capability could not see where the text went. It reaches the trace
    (``steps/NNN.json``) for dataset export to use; no agent renders it into
    a prompt (docs/architecture_design.md §5.6).

    ``effect`` is what the action measurably did (:class:`ActionEffect`),
    independent of ``success``: a failed measurement makes a fact unknown and
    never changes the outcome. ``None``: the action declares no effect, or it
    did not get as far as anything measurable. It is serialized whole, as its
    concrete type, and read back as that type by the trace reader.

    ``success`` is whether the action executed, for every action, the one
    that ends the run included: a done result's ``success`` says the run was
    closed, never whether the task was accomplished. The ending action's
    payload is the three fields after it: ``is_done`` closes the run,
    ``extracted_content`` is the answer, and ``reported_success`` is what the
    agent said about the task: ``True`` (accomplished), ``False`` (not
    accomplished) or ``None`` (it said neither). It is the agent's own claim,
    not a verdict; the Loop copies it into ``AgentResult.agent_reported_success``
    (``docs/architecture_design.md`` §4.1, "agent 的任务结论").
    """

    success: bool
    error: str | None = None
    is_done: bool = False
    extracted_content: str | None = None
    #: Left out of the serialized result when ``None``, like ``effect``: only a
    #: done result can carry one, so every other result writes what it always
    #: wrote, and a missing value and "the agent made no claim" are one fact.
    reported_success: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    typed_into_sensitive_field: bool | None = None
    #: Left out of the serialized result when ``None``, so a result without an
    #: effect writes what it always wrote: a missing ``effect`` and "this action
    #: reports none" are the same fact.
    effect: SerializeAsAny[ActionEffect] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _a_claim_ends_the_run(self) -> "ActionResult":
        """Only the action that ends the run carries the agent's claim about the task."""
        if self.reported_success is not None and not self.is_done:
            raise ValueError(
                "reported_success is the agent's claim about the task, carried by the action "
                "that ends the run; a result with is_done=False cannot carry one"
            )
        return self

    @field_validator("effect", mode="before")
    @classmethod
    def _effect_is_typed(cls, value: Any) -> Any:
        """An effect arrives as its concrete model, never as a bare mapping.

        Only the action registry knows which class an ``effect_id`` names, and
        this package does not import it. A recorded effect is rebuilt by the
        trace reader (``core/trace/effects.py``) before the result is
        validated; a mapping reaching here was not, and would otherwise be
        read as the base class or refused for its unknown fields.
        """
        if isinstance(value, dict):
            raise ValueError(
                "an action effect must be an ActionEffect instance; a recorded effect is "
                "read back by the trace reader, which resolves its effect_id through the "
                "action registry"
            )
        return value


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

    Two more fields say what some of those params *mean*, rendered from the
    same ``Params`` markers (``core/actions/params.py``) and narrowed with
    them: ``point_params``, the ``(x, y)`` pairs that are one point in the
    capability's viewport units (``PointAxis``), and ``element_params``, the
    params that name an element of an observation fragment
    (``AddressesElement``). An Agent reads them here, off the action space it
    was handed, rather than restating which params of which verb are points.

    There is deliberately no conversion to a provider function-calling
    schema here; see ``docs/action_descriptor_vs_tool_definition.md``.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    params: dict[str, str] = Field(default_factory=dict)
    params_schema: dict[str, Any] | None = None
    point_params: tuple[tuple[str, str], ...] = ()
    element_params: tuple[str, ...] = ()


class Action(BaseModel):
    """Parsed action from LLM output."""

    name: str
    params: dict[str, Any] = Field(default_factory=dict)
