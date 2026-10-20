"""Action registrar — conscribe-driven auto-registration.

Each concrete Action class sets ``__registry_key__`` and can declare
capability-side configuration fields as ``Annotated[type, Field(...)]``.
Symmetric to :class:`spectragent.schemas.observation._registrar.FragmentBase`,
but uses a ``@dataclass`` base (no pydantic bridge) because actions are
executors, not data-transfer objects.

Reference pattern: :class:`spectragent.agents._registrar.AgentBase`, which
also combines ``@dataclass`` + conscribe metaclass + ``Annotated`` fields.

See docs/architecture_design.md §4.5.
"""

from __future__ import annotations

from typing import Any, ClassVar

from conscribe import create_registrar
from pydantic import BaseModel

from spectragent.core.actions.base import BaseAction
from spectragent.core.actions.params import action_fragments, params_view
from spectragent.schemas.action import ActionDescriptor, ActionEffect

#: Separator between the owning capability and the local action id.
#:
#: Declaring it is what lets conscribe expand a capability-relative
#: ``__wiring__`` entry (``"click"``) into every provider that offers it, and
#: what lets negotiation project the environment's fully-qualified keys down
#: to the short names an agent declares. It does not change the
#: discriminator: keys stay flat and fully qualified everywhere else —
#: registration, routing, descriptors, the trace.
ACTION_KEY_SEPARATOR = "."

ActionRegistrar = create_registrar(
    "action",
    BaseAction,
    discriminator_field="action_id",
    key_separator=ACTION_KEY_SEPARATOR,
)


class ActionBase(metaclass=ActionRegistrar.Meta):
    """Concrete base for all action implementations.

    Subclasses set ``__registry_key__`` to a fully-qualified dotted key —
    ``"{source}.{local_id}"`` (e.g. ``"browser.click"``,
    ``"tool_sandbox.call_tool"``). The prefix makes registration across
    capabilities collision-free (``browser.click`` vs ``desktop.click``).
    :attr:`action_id` returns the full key (which is what environments
    route on) and :attr:`source` returns the prefix segment for callers
    that need the owning-capability identity.

    **Consumers do not have to spell the prefix out.** An agent needing a
    click says ``"click"`` in its ``__wiring__``; conscribe widens that
    to every registered provider at config-generation time, and
    negotiation binds it to the one this run holds (or refuses two).
    Writing ``"browser.click"`` instead pins that provider. Either way
    the key that leaves negotiation is fully qualified.

    Subclasses should use ``@dataclass`` + ``Annotated[type, Field(...)]``
    fields to expose capability-side configuration to conscribe codegen.

    **Every action declares its parameters as a typed model**, ``Params``
    — flat, scalar, ``extra="forbid"``. It is the single source of the
    descriptor's ``params_schema`` (what a provider constrains on), the
    descriptor's ``params`` prose (what the prompt shows) and the
    action's observation-fragment dependencies
    (:class:`~spectragent.core.actions.params.RequiresFragment`). Nothing
    hand-writes any of the three; :meth:`describe` renders them together.

    Example::

        from dataclasses import dataclass
        from typing import Annotated, Literal
        from pydantic import BaseModel, ConfigDict, Field as PydanticField

        @dataclass
        class ScrollAction(ActionBase):
            __registry_key__ = "browser.scroll"

            class Params(BaseModel):
                model_config = ConfigDict(extra="forbid")
                direction: Literal["up", "down", "left", "right"]
                index: Annotated[int, RequiresFragment("browser.dom")] | None = None

            default_amount: Annotated[
                int, PydanticField(description="Default scroll px")
            ] = 300

            def descriptor(self, **context):
                return self.describe(
                    f"Scroll the page (default {self.default_amount}px).",
                    **context,
                )

            async def execute(self, params, *, capability):
                ...
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    Params: ClassVar[type[BaseModel]]
    """This action's parameter contract. Declared by every concrete action.

    Annotation-only on the base so an action that forgets one fails loudly
    on first use instead of silently advertising no parameters;
    ``tests/test_core/test_actions/test_action_params_schema.py`` scans the
    registry for it.
    """

    Effect: ClassVar[type[ActionEffect] | None] = None
    """What this action measurably did, as a model — the output twin of ``Params``.

    ``None``: the action reports no effect (``done``, ``screenshot``). A
    concrete action names a subclass of
    :class:`~spectragent.schemas.action.ActionEffect`, possibly shared with
    sibling actions, measures it during ``execute`` and returns it in
    ``ActionResult.effect`` with ``effect_id`` set to its own
    :attr:`action_id`. The trace reader finds this attribute by that key to
    read a recorded effect back as its type (``core/trace/effects.py``;
    ``docs/architecture_design.md`` §4.5, "动作效果").
    """

    @property
    def action_id(self) -> str:
        """Fully-qualified registry key (e.g. ``"browser.click"``)."""
        return self.__registry_key__

    @property
    def source(self) -> str:
        """First segment of ``__registry_key__`` — the owning capability."""
        return self.__registry_key__.split(".", 1)[0]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        """This action as the prompt and the schema see it.

        Every concrete action overrides it, almost always by delegating to
        :meth:`describe`. Declared here (rather than only on the
        :class:`~spectragent.core.actions.base.BaseAction` Protocol) so a
        capability holding ``list[ActionBase]`` can call it.
        """
        raise NotImplementedError(f"{self.__registry_key__} must implement descriptor()")

    def requires_fragments(self) -> frozenset[str]:
        """Observation fragments this action cannot run without.

        Derived from the ``Params`` fields that are *required* and carry a
        :class:`~spectragent.core.actions.params.RequiresFragment` marker,
        so a capability declares the dependency once, on the parameter
        that has it.
        """
        return action_fragments(self.Params)

    def describe(
        self,
        description: str,
        *,
        hints: dict[str, str] | None = None,
        fragments: frozenset[str] | None = None,
        **_context: Any,
    ) -> ActionDescriptor:
        """Build this action's descriptor from :attr:`Params`.

        Args:
            description: What the action does, in the prompt's words.
            hints: Param name → the current value of the capability-side
                knob governing it (``{"clear": "default=true"}``). YAML
                knobs reach the model through the param's description;
                they are never emitted as a schema ``default``, because
                the default is the capability's to apply.
            fragments: The run's active observation fragments. Optional
                params whose fragment is missing are dropped from the
                descriptor, so the prompt cannot teach a parameter this
                run has no way to honour.
            _context: The rest of ``get_action_space(**context)``. An
                action that needs more of it overrides ``descriptor``.
        """
        prose, schema = params_view(self.Params, hints=hints, fragments=fragments)
        return ActionDescriptor(
            name=self.action_id,
            description=description,
            params=prose,
            params_schema=schema,
        )
