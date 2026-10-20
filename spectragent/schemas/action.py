"""Action-related data contracts.

See docs/architecture_design.md §4.5 for the full Action design.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator

#: A fact an action effect could not report as a value. ``"unknown"``: measuring
#: it was attempted and failed or ran out of the measurement budget.
#: ``"unavailable"``: it cannot be measured here (the platform has no such
#: fact, or it lies where the capability cannot look). Neither is ``None`` or
#: ``0``: a measured zero is a value, and the three stay distinct in the trace
#: and in :meth:`ActionEffect.describe`.
Unmeasured = Literal["unknown", "unavailable"]


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
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect_id: str = Field(min_length=1)

    def describe(self) -> str:
        """One deterministic English line of the measured facts, for a prompt."""
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
    """

    success: bool
    error: str | None = None
    is_done: bool = False
    extracted_content: str | None = None
    typed_into_sensitive_field: bool | None = None
    #: Left out of the serialized result when ``None``, so a result without an
    #: effect writes what it always wrote: a missing ``effect`` and "this action
    #: reports none" are the same fact.
    effect: SerializeAsAny[ActionEffect] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

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
